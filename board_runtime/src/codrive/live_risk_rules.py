"""Live DMS timing and CARLA acceleration extensions retained during core migration."""

from dataclasses import dataclass, field
from .models import AudioKind, Severity, SituationSnapshot, TrafficLight
from .driver_attention import DriverAttentionGate
from .vehicle_dynamics import SpeedAcceleration
from .temporal_hold import CaptureHold


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    object_min_confidence: float = 0.5
    pedestrian_critical_ttc_s: float = 1.8
    pedestrian_warning_ttc_s: float = 3.5
    lane_warning_offset_m: float = 0.45
    lane_critical_offset_m: float = 0.85
    minimum_motion_mps: float = 1.4
    distraction_threshold: float = 0.35
    hard_acceleration_mps2: float = 4.0
    accelerator_mismatch_pct: float = 90.0
    acceleration_confirm_ms: int = 500
    imminent_object_ttc_s: float = 4.0
    imminent_object_distance_m: float = 15.0
    duplicate_cooldown_ms: int = 900
    event_ttl_ms: int = 1_500
    cabin_observation_enabled: bool = False
    audio_alerts_enabled: bool = True
    can_speed_acceleration_enabled: bool = False
    full_pedal_warning_enabled: bool = False
    full_pedal_threshold_pct: float = 90.
    full_pedal_confirm_ms: int = 300
    driver_drowsy_confirm_ms: int = 2_000
    driver_distraction_confirm_ms: int = 3_000
    driver_recovery_ms: int = 1_000


@dataclass(frozen=True, slots=True)
class _Candidate:
    code: str
    severity: Severity
    score: float
    action: str
    message_key: str
    evidence: tuple[str, ...]


@dataclass
class LiveRules:
    policy: RiskPolicy
    driver_attention: DriverAttentionGate = field(init=False)
    speed_acceleration: SpeedAcceleration = field(default_factory=SpeedAcceleration)

    acceleration_hold: CaptureHold = field(init=False)
    full_pedal_hold: CaptureHold = field(init=False)

    def __post_init__(self):
        self.full_pedal_hold=CaptureHold(self.policy.full_pedal_confirm_ms,750)
        self.acceleration_hold = CaptureHold(self.policy.acceleration_confirm_ms, 250)
        self.driver_attention = DriverAttentionGate(
            self.policy.driver_drowsy_confirm_ms,
            self.policy.driver_distraction_confirm_ms,
            self.policy.driver_recovery_ms,
            attention_threshold=self.policy.distraction_threshold,
        )

    def _driver_and_audio(self, snapshot: SituationSnapshot) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        driver = snapshot.driver
        moving = (
            snapshot.vehicle is not None
            and snapshot.vehicle.speed_mps >= self.policy.minimum_motion_mps
        )
        if (
            driver
            and driver.face_detected
            and moving
            and (self.driver_attention.active_behavior == "distracted" or driver.phone_detected)
        ):
            external_hazard = any(
                obj.confidence >= self.policy.object_min_confidence
                and obj.zone in {"rear", "left", "right", "blind_spot"}
                and (
                    (obj.ttc_s is not None and obj.ttc_s <= self.policy.imminent_object_ttc_s)
                    or (
                        obj.distance_m is not None
                        and obj.distance_m <= self.policy.imminent_object_distance_m
                    )
                )
                for item in snapshot.external
                for obj in item.objects
            )
            severity = Severity.CRITICAL if external_hazard else Severity.WARNING
            candidates.append(
                _Candidate(
                    code="driver_inattention",
                    severity=severity,
                    score=severity * 100.0 + (1.0 - driver.attention_score) * 10.0,
                    action="look_forward",
                    message_key="look_forward",
                    evidence=(f"attention={driver.attention_score:.2f}",),
                )
            )
        audio = snapshot.audio
        if (
            driver
            and driver.face_detected
            and moving
            and self.driver_attention.active_behavior == "drowsy"
        ):
            candidates.append(
                _Candidate(
                    code="driver_drowsy_observation",
                    severity=Severity.WARNING,
                    score=210.0,
                    action="rest_safely",
                    message_key="drowsy_observation",
                    evidence=(f"confirmed_ms={self.policy.driver_drowsy_confirm_ms}",),
                )
            )
        # Cabin-only operation can report model observations without inventing
        # vehicle speed or claiming a driving hazard. Full rules retain priority.
        if self.policy.cabin_observation_enabled and driver and driver.face_detected:
            observation = None
            if driver.behavior == "phone" or (driver.behavior is None and driver.phone_detected):
                observation = ("driver_phone_observation", "phone_observation", 115.0)
            elif self.driver_attention.active_behavior == "drowsy":
                observation = ("driver_drowsy_observation", "drowsy_observation", 110.0)
            elif self.driver_attention.active_behavior == "distracted":
                observation = ("driver_attention_observation", "attention_observation", 105.0)
            if observation:
                code, key, score = observation
                candidates.append(
                    _Candidate(
                        code=code,
                        severity=Severity.INFO,
                        score=score,
                        action="observe_driver",
                        message_key=key,
                        evidence=("cabin_model_observation", f"model={driver.model_version}"),
                    )
                )
        if (
            self.policy.audio_alerts_enabled
            and audio
            and audio.kind is AudioKind.SIREN
            and audio.confidence >= 0.7
        ):
            yellow = any(item.traffic_light is TrafficLight.YELLOW for item in snapshot.external)
            candidates.append(
                _Candidate(
                    code="emergency_vehicle",
                    severity=Severity.WARNING,
                    score=235.0 if yellow else 225.0,
                    action="yield_safely",
                    message_key="siren_yield",
                    evidence=(f"confidence={audio.confidence:.2f}", f"yellow_light={yellow}"),
                )
            )
        return candidates

    def _full_pedal(self, snapshot):
        vehicle=snapshot.vehicle
        if not self.policy.full_pedal_warning_enabled or vehicle is None or not vehicle.can_valid:
            self.full_pedal_hold.clear();return []
        now=snapshot.header.capture_time_ns
        times=dict(vehicle.signal_times_ns)
        fresh=all(name in times and 0<=now-times[name]<=750_000_000
                  for name in ('accelerator_pct','brake_pressed'))
        active=fresh and vehicle.accelerator_pct>=self.policy.full_pedal_threshold_pct and not vehicle.brake_pressed
        stamp=times.get('accelerator_pct',vehicle.header.capture_time_ns)
        if not self.full_pedal_hold.update('full_pedal',stamp,active,
                (vehicle.header.source_id,vehicle.header.boot_id)):return []
        return [_Candidate('full_accelerator',Severity.WARNING,70.,'check_accelerator','full_accelerator',
            (f'accelerator_pct={vehicle.accelerator_pct:.1f}',
             f'confirmed_ms={self.policy.full_pedal_confirm_ms}','source=pedal_only','acceleration_not_measured'))]

    def _acceleration_mismatch(self, snapshot: SituationSnapshot) -> list[_Candidate]:
        vehicle, ins = snapshot.vehicle, snapshot.ins
        if self.policy.can_speed_acceleration_enabled:
            acceleration = self.speed_acceleration.acceleration_mps2
            code = "rapid_acceleration"
            evidence_source = "carla_speed_over_can"
        else:
            acceleration = ins.longitudinal_accel_mps2 if ins else None
            code = "unintended_acceleration"
            evidence_source = "ins"
        if vehicle is None or acceleration is None:
            self.acceleration_hold.clear()
            return []
        active = (
            vehicle.accelerator_pct >= self.policy.accelerator_mismatch_pct
            and not vehicle.brake_pressed
            and acceleration + 1e-6 >= self.policy.hard_acceleration_mps2
        )
        if self.acceleration_hold.update("acceleration", vehicle.header.capture_time_ns, active,
                (vehicle.header.source_id, vehicle.header.boot_id)):
            return [
                _Candidate(
                    code=code,
                    severity=Severity.CRITICAL,
                    score=340.0 + acceleration,
                    action="release_accelerator",
                    message_key="release_accelerator",
                    evidence=(
                        f"accelerator_pct={vehicle.accelerator_pct:.1f}",
                        f"acceleration_mps2={acceleration:.2f}",
                        f"acceleration_source={evidence_source}",
                        f"confirmed_ms={self.policy.acceleration_confirm_ms}",
                    ),
                )
            ]
        return []
