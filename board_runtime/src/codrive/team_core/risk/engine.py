"""Explainable snapshot-to-risk rules, independent of sensor transport."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field, replace

from ..situation.fusion import FusionPolicy
from ..contracts.models import (
    AudioKind, DriverState, Header, RiskAssessment, RiskCandidate, RiskEvent,
    RiskLevel, SituationSnapshot, TrackedObject, TrafficLight, TurnSignal,
)
from .fsm import RiskStateMachine
from ...temporal_hold import CaptureHold
from .policy import RiskPolicy, load_risk_policy


_CLASS_ALIASES = {
    "person": "pedestrian", "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
    "bike": "bicycle", "cyclist": "bicycle", "pm": "bicycle", "scooter": "bicycle",
    "motorbike": "motorcycle", "static_obstacle": "obstacle",
}


@dataclass(slots=True)
class RiskEngine:
    source_id: str = "pi5.risk"
    policy: RiskPolicy = field(default_factory=RiskPolicy)
    freshness: FusionPolicy = field(default_factory=FusionPolicy)
    _sequence: int = 0
    _fsm: RiskStateMachine = field(init=False)
    _lane_hold: CaptureHold = field(init=False)
    _last_emitted_ns: dict[tuple[str, str], int] = field(default_factory=dict)
    _last_active_identity: tuple[str, str] | None = None
    _last_active_level: RiskLevel = RiskLevel.NORMAL
    _eyes_closed_since_ns: int | None = None
    _driver_identity: tuple[str, str, str] | None = None
    _last_driver_capture_ns: int | None = None
    _last_assessed_ns: int | None = None

    def __post_init__(self) -> None:
        self._lane_hold = CaptureHold(self.policy.lane_confirm_ms, 750)
        self._fsm = RiskStateMachine(
            caution_exit_score=self.policy.caution_exit_score,
            warning_exit_score=self.policy.warning_exit_score,
            emergency_exit_score=self.policy.emergency_exit_score,
            release_hold_ms=self.policy.release_hold_ms,
            state_gap_reset_ms=self.policy.state_gap_reset_ms,
        )

    def level_for_score(self, score: float) -> RiskLevel:
        if (isinstance(score, bool) or not isinstance(score, (float, int))
                or not math.isfinite(score) or score < 0):
            raise ValueError("score must be a finite non-negative number")
        if score >= self.policy.emergency_score:
            return RiskLevel.EMERGENCY
        if score >= self.policy.warning_score:
            return RiskLevel.WARNING
        if score >= self.policy.caution_score:
            return RiskLevel.CAUTION
        return RiskLevel.NORMAL

    def _fresh_snapshot(self, snapshot: SituationSnapshot, now_ns: int) -> SituationSnapshot:
        stale = list(snapshot.stale_sources)

        def fresh(value, ttl_ms: int, name: str):
            if value is not None:
                age = now_ns - value.header.capture_time_ns
                if 0 <= age <= ttl_ms * 1_000_000:
                    return value
                stale.append(name)
            return None

        vehicle = fresh(snapshot.vehicle, self.freshness.vehicle_ttl_ms, "vehicle")
        if vehicle is not None and not vehicle.can_valid:
            vehicle = None
            stale.append("vehicle:invalid_can")
        external = tuple(
            value for item in snapshot.external
            if (value := fresh(item, self.freshness.external_ttl_ms,
                               f"camera:{item.camera_id}")) is not None
        )
        return replace(
            snapshot, vehicle=vehicle, external=external,
            driver=fresh(snapshot.driver, self.freshness.driver_ttl_ms, "driver"),
            audio=fresh(snapshot.audio, self.freshness.audio_ttl_ms, "audio"),
            ins=fresh(snapshot.ins, self.freshness.ins_ttl_ms, "ins"),
            stale_sources=tuple(dict.fromkeys(stale)),
        )

    def _aligned(self, *values) -> bool:
        if any(value is None for value in values):
            return False
        times = [value.header.capture_time_ns for value in values]
        return max(times) - min(times) <= self.policy.max_context_skew_ms * 1_000_000

    def _driver_context(self, driver: DriverState | None) -> tuple[float, str | None]:
        if driver is None or not driver.face_detected:
            self._eyes_closed_since_ns = None
            self._last_driver_capture_ns = None
            self._driver_identity = None
            return 1.0, None
        identity = (driver.header.source_id, driver.header.boot_id, driver.camera_id)
        captured = driver.header.capture_time_ns
        if (identity != self._driver_identity or self._last_driver_capture_ns is None
                or captured < self._last_driver_capture_ns
                or captured - self._last_driver_capture_ns
                > self.freshness.driver_ttl_ms * 1_000_000):
            self._eyes_closed_since_ns = None
        self._driver_identity = identity
        self._last_driver_capture_ns = captured
        if not driver.eyes_closed:
            self._eyes_closed_since_ns = None
        elif self._eyes_closed_since_ns is None:
            self._eyes_closed_since_ns = captured
        sustained_closure = (
            self._eyes_closed_since_ns is not None
            and captured - self._eyes_closed_since_ns >= self.policy.eyes_closed_hold_ms * 1_000_000
        )
        drowsy = sustained_closure or (
            driver.drowsiness_score is not None
            and driver.drowsiness_score >= self.policy.drowsiness_threshold
        )
        inattentive = (not driver.looking_forward
                       or driver.attention_score < self.policy.distraction_threshold)
        if drowsy:
            return (self.policy.drowsy_inattention_weight if inattentive
                    else self.policy.drowsiness_weight), "driver_drowsiness"
        if driver.phone_detected:
            return self.policy.phone_weight, "driver_phone_use"
        if inattentive:
            return self.policy.inattention_weight, "driver_inattention"
        return 1.0, None

    def _distance_weight(self, distance: float | None) -> float:
        if distance is None:
            return 1.0
        if distance <= self.policy.near_distance_m:
            return self.policy.near_distance_weight
        if distance <= self.policy.middle_distance_m:
            return self.policy.middle_distance_weight
        if distance <= self.policy.far_distance_m:
            return self.policy.far_distance_weight
        return self.policy.distant_weight

    def _ttc(self, obj: TrackedObject) -> tuple[float | None, str]:
        # A known receding target invalidates an inconsistent supplied TTC.
        if (obj.relative_speed_mps is not None
                and obj.relative_speed_mps <= self.policy.closing_speed_epsilon_mps):
            return None, "not_closing"
        if obj.ttc_s is not None:
            return obj.ttc_s, "supplied"
        if obj.distance_m is not None and obj.relative_speed_mps is not None:
            return obj.distance_m / obj.relative_speed_mps, "relative_speed"
        return None, "unavailable"

    def _candidate(self, code: str, target: str, score: float, action: str,
                   message_key: str, evidence: tuple[str, ...], **context) -> RiskCandidate:
        return RiskCandidate(
            code=code, target_id=target, level=self.level_for_score(score), score=score,
            recommended_action=action, message_key=message_key, evidence=evidence, **context,
        )

    def _environment_context(self, snapshot):
        """The live adapter supplies validated API context; standalone core is neutral."""
        return 1.0, ()

    def _objects(self, snapshot: SituationSnapshot, driver_weight: float,
                 unavailable: list[str]) -> list[RiskCandidate]:
        candidates = []
        environment_weight, environment_evidence = self._environment_context(snapshot)
        for perception in snapshot.external:
            vehicle = snapshot.vehicle if self._aligned(snapshot.vehicle, perception) else None
            weight = driver_weight if self._aligned(snapshot.driver, perception) else 1.0
            for obj in perception.objects:
                if obj.confidence < self.policy.object_min_confidence:
                    continue
                kind = obj.class_name.lower()
                kind = _CLASS_ALIASES.get(kind, kind)
                if kind not in self.policy.object_base_scores:
                    unavailable.append(f"object_class:{obj.class_name}")
                    continue
                direction = obj.zone.lower().replace(" ", "_")
                if direction not in self.policy.direction_weights:
                    unavailable.append(f"object_direction:{perception.camera_id}:{obj.track_id}")
                    continue
                ttc, ttc_source = self._ttc(obj)
                if obj.distance_m is None and ttc is None:
                    unavailable.append(f"object_distance:{perception.camera_id}:{obj.track_id}")
                    continue
                in_path = obj.in_path if obj.in_path is not None else direction == "front"
                base = self.policy.object_base_scores[kind]
                distance_weight = self._distance_weight(obj.distance_m)
                direction_weight = self.policy.direction_weights[direction]
                vehicle_weight = 1.0
                if vehicle is not None:
                    if vehicle.speed_mps > self.policy.high_speed_mps:
                        vehicle_weight *= self.policy.high_speed_weight
                    if vehicle.brake_pressed:
                        vehicle_weight *= self.policy.braking_weight
                score = base * distance_weight * direction_weight * vehicle_weight * weight * environment_weight
                evidence = [f"camera={perception.camera_id}", f"class={kind}",
                            f"confidence={obj.confidence:.2f}", f"in_path={in_path}",
                            f"ttc_source={ttc_source}", f"weighted_score={score:.2f}",
                            f"environment_weight={environment_weight:.2f}", *environment_evidence]
                if ttc is not None:
                    evidence.append(f"ttc_s={ttc:.2f}")
                if ttc is not None and in_path:
                    if ttc < self.policy.collision_emergency_ttc_s:
                        score = max(score, self.policy.emergency_score)
                    elif ttc < self.policy.collision_warning_ttc_s:
                        score = max(score, self.policy.warning_score)
                    elif ttc <= self.policy.collision_caution_ttc_s:
                        score = max(score, self.policy.caution_score)
                if ttc_source == "not_closing":
                    score = min(score, math.nextafter(self.policy.warning_score, 0.0))
                    evidence.append("capped=not_closing")
                elif not in_path or (ttc is None and vehicle is None):
                    score = min(score, math.nextafter(self.policy.emergency_score, 0.0))
                    evidence.append("capped=path_or_vehicle_unknown")
                if (vehicle is not None and vehicle.speed_mps < self.policy.minimum_motion_mps
                        and ttc is None):
                    score = min(score, math.nextafter(self.policy.warning_score, 0.0))
                    evidence.append("capped=ego_stationary")
                level = self.level_for_score(score)
                action = "brake_now" if level is RiskLevel.EMERGENCY else "slow_down"
                if not in_path:
                    action = "check_surroundings"
                code = "pedestrian_collision" if kind == "pedestrian" else "object_collision"
                message_key = "pedestrian_brake" if kind == "pedestrian" else "object_brake"
                if level is not RiskLevel.EMERGENCY:
                    message_key = "object_slow" if in_path else "check_surroundings"
                candidates.append(self._candidate(
                    code, f"{perception.header.source_id}:{perception.camera_id}:{obj.track_id}",
                    score, action, message_key, tuple(evidence), direction=direction,
                    distance_m=obj.distance_m, ttc_s=ttc, base_score=base,
                    distance_weight=distance_weight, direction_weight=direction_weight,
                    vehicle_weight=vehicle_weight, driver_weight=weight,
                ))
        return candidates

    def _road(self, snapshot: SituationSnapshot, driver_weight: float,
              unavailable: list[str]) -> list[RiskCandidate]:
        candidates = []
        environment_weight, environment_evidence = self._environment_context(snapshot)
        environment_evidence = (f"environment_weight={environment_weight:.2f}", *environment_evidence)
        vehicle = snapshot.vehicle
        if vehicle is None:
            self._lane_hold.clear()
            unavailable.extend(("signal:vehicle_missing", "lane:vehicle_missing"))
            return candidates
        for perception in snapshot.external:
            if perception.camera_id not in self.policy.road_camera_ids:
                continue
            target = f"{perception.header.source_id}:{perception.camera_id}"
            if not self._aligned(vehicle, perception):
                self._lane_hold.clear((target,"warning"));self._lane_hold.clear((target,"critical"))
                unavailable.append(f"road:time_skew:{perception.camera_id}")
                continue
            if vehicle.speed_mps < self.policy.minimum_motion_mps:
                self._lane_hold.clear((target,"warning"));self._lane_hold.clear((target,"critical"))
                continue
            weight = driver_weight if self._aligned(snapshot.driver, vehicle, perception) else 1.0
            target = f"{perception.header.source_id}:{perception.camera_id}"
            if (perception.traffic_light_confidence >= self.policy.signal_min_confidence
                    and not vehicle.brake_pressed):
                if perception.traffic_light is TrafficLight.RED:
                    score = self.policy.red_light_base_score + (
                        self.policy.red_light_speed_bonus if vehicle.speed_mps >
                        self.policy.red_light_high_speed_mps else 0.0
                    )
                    candidates.append(self._candidate(
                        "red_light_approach", target, score * weight * environment_weight, "brake_now",
                        "red_light_brake", (f"speed_mps={vehicle.speed_mps:.2f}", "brake=false", *environment_evidence),
                        direction="front", base_score=score, driver_weight=weight,
                    ))
                elif (perception.traffic_light is TrafficLight.YELLOW
                      and vehicle.speed_mps > self.policy.yellow_light_high_speed_mps):
                    candidates.append(self._candidate(
                        "yellow_light_approach", target,
                        self.policy.yellow_light_base_score * weight * environment_weight, "slow_down",
                        "yellow_light_slow", (f"speed_mps={vehicle.speed_mps:.2f}", *environment_evidence),
                        direction="front", base_score=self.policy.yellow_light_base_score,
                        driver_weight=weight,
                    ))
            offset = perception.lane_offset_m
            valid = (offset is not None and perception.lane_confidence >= self.policy.lane_min_confidence
                     and vehicle.turn_signal is TurnSignal.OFF and not vehicle.hazard_lights)
            magnitude = abs(offset) if offset is not None else 0
            identity = (perception.header.boot_id, "right" if offset is not None and offset > 0 else "left")
            stamp = perception.header.capture_time_ns
            warning = self._lane_hold.update((target,"warning"),stamp,
                valid and magnitude >= self.policy.lane_warning_offset_m,identity)
            critical = self._lane_hold.update((target,"critical"),stamp,
                valid and magnitude >= self.policy.lane_critical_offset_m,identity)
            if not warning:
                continue
            score = self.policy.emergency_score if critical else self.policy.warning_score
            candidates.append(self._candidate(
                "lane_departure", target, score * weight * environment_weight, "return_to_lane", "lane_return",
                (f"offset_m={offset:.2f}", f"confidence={perception.lane_confidence:.2f}", f"confirmed_ms={self.policy.lane_confirm_ms}", *environment_evidence),
                direction="right" if offset > 0 else "left", base_score=score, driver_weight=weight,
            ))
        return candidates

    def _driver_and_audio(self, snapshot: SituationSnapshot, driver_weight: float,
                          driver_code: str | None) -> list[RiskCandidate]:
        candidates = []
        vehicle = snapshot.vehicle
        driver = snapshot.driver
        driver_vehicle = vehicle if self._aligned(driver, vehicle) else None
        if (driver is not None and driver_code is not None
                and (driver_vehicle is None
                     or driver_vehicle.speed_mps >= self.policy.minimum_motion_mps)):
            score = {
                "driver_drowsiness": self.policy.driver_drowsiness_score,
                "driver_phone_use": self.policy.driver_phone_score,
                "driver_inattention": self.policy.driver_inattention_score,
            }[driver_code]
            action = {"driver_drowsiness": "stop_safely", "driver_phone_use": "put_down_phone",
                      "driver_inattention": "look_forward"}[driver_code]
            candidates.append(self._candidate(
                driver_code, f"{driver.header.source_id}:{driver.camera_id}", score,
                action,
                driver_code if driver_code != "driver_inattention" else "look_forward",
                (f"attention={driver.attention_score:.2f}",
                 f"phone={driver.phone_detected}", f"drowsiness={driver.drowsiness_score}",
                 f"external_risk_multiplier={driver_weight}"),
                base_score=score,
            ))
        audio = snapshot.audio
        if audio is not None and audio.confidence >= self.policy.audio_min_confidence:
            if audio.kind in {AudioKind.SIREN, AudioKind.HORN}:
                score = (self.policy.siren_base_score if audio.kind is AudioKind.SIREN
                         else self.policy.horn_base_score)
                if (self._aligned(audio, vehicle)
                        and vehicle.speed_mps > self.policy.high_speed_mps):
                    score += self.policy.audio_speed_bonus
                    if audio.direction.lower() in {
                        "rear", "left", "right", "rear_left", "rear_right"
                    }:
                        score += self.policy.audio_direction_bonus
                code = "emergency_vehicle" if audio.kind is AudioKind.SIREN else "horn_alert"
                candidates.append(self._candidate(
                    code, audio.header.source_id, score,
                    "yield_safely" if audio.kind is AudioKind.SIREN else "check_surroundings",
                    "siren_yield" if audio.kind is AudioKind.SIREN else "check_surroundings",
                    (f"confidence={audio.confidence:.2f}",), direction=audio.direction,
                    base_score=score,
                ))
        return candidates

    def _acceleration(self, snapshot: SituationSnapshot) -> list[RiskCandidate]:
        vehicle, ins = snapshot.vehicle, snapshot.ins
        if (not self._aligned(vehicle, ins) or ins.longitudinal_accel_mps2 is None
                or vehicle.accelerator_pct < self.policy.accelerator_mismatch_pct
                or vehicle.brake_pressed
                or ins.longitudinal_accel_mps2 < self.policy.hard_acceleration_mps2):
            return []
        return [self._candidate(
            "unintended_acceleration", vehicle.header.source_id, self.policy.emergency_score + 10,
            "release_accelerator", "release_accelerator",
            (f"accelerator_pct={vehicle.accelerator_pct:.1f}",
             f"ins_accel_mps2={ins.longitudinal_accel_mps2:.2f}"),
            base_score=self.policy.emergency_score + 10,
        )]

    def _priority(self, candidate: RiskCandidate) -> tuple:
        imminent_collision = (
            candidate.code in {"pedestrian_collision", "object_collision"}
            and candidate.level is RiskLevel.EMERGENCY and candidate.ttc_s is not None
            and candidate.ttc_s < self.policy.collision_emergency_ttc_s
        )
        priorities = {"unintended_acceleration": 6, "pedestrian_collision": 5,
                      "object_collision": 4, "red_light_approach": 3, "lane_departure": 2}
        return (candidate.level.rank, imminent_collision,
                -candidate.ttc_s if imminent_collision else 0.0, candidate.score,
                priorities.get(candidate.code, 1), candidate.target_id)

    def _event(self, active: RiskCandidate | None, level: RiskLevel, header: Header,
               now_ns: int) -> RiskEvent | None:
        if active is None:
            self._last_active_identity = None
            self._last_active_level = RiskLevel.NORMAL
            return None
        key = active.identity
        last = self._last_emitted_ns.get(key)
        escalation = level.rank > self._last_active_level.rank
        changed_target = key != self._last_active_identity
        self._last_active_identity = key
        self._last_active_level = level
        if (last is not None and not escalation and not changed_target
                and now_ns - last < self.policy.duplicate_cooldown_ms * 1_000_000):
            return None
        self._last_emitted_ns[key] = now_ns
        # Bound per-target warning history during long drives.
        history_ms = max(self.policy.duplicate_cooldown_ms, self.policy.event_ttl_ms)
        cutoff = now_ns - history_ms * 1_000_000
        for old_key, emitted in list(self._last_emitted_ns.items()):
            if emitted < cutoff:
                del self._last_emitted_ns[old_key]
        return RiskEvent(
            header=header, event_id=uuid.uuid4().hex, code=active.code,
            severity=level.severity, score=active.score,
            recommended_action=active.recommended_action, message_key=active.message_key,
            evidence=active.evidence, expires_at_ns=now_ns + self.policy.event_ttl_ms * 1_000_000,
        )

    def assess(self, snapshot: SituationSnapshot, *, now_ns: int | None = None) -> RiskAssessment:
        """Always return state, including NORMAL, missing data, and suppressed events."""
        now_ns = time.time_ns() if now_ns is None else now_ns
        if self._last_assessed_ns is not None and now_ns < self._last_assessed_ns:
            raise ValueError("risk evaluation time must not move backwards")
        snapshot = self._fresh_snapshot(snapshot, now_ns)
        unavailable: list[str] = []
        if not snapshot.external:
            unavailable.append("external_perception")
        if snapshot.vehicle is None:
            unavailable.append("vehicle_context")
        if snapshot.driver is None or not snapshot.driver.face_detected:
            unavailable.append("driver_context")
        if snapshot.ins is None or snapshot.ins.longitudinal_accel_mps2 is None:
            unavailable.append("acceleration_cross_check")
        weight, driver_code = self._driver_context(snapshot.driver)
        candidates = (
            self._objects(snapshot, weight, unavailable)
            + self._road(snapshot, weight, unavailable)
            + self._driver_and_audio(snapshot, weight, driver_code)
            + self._acceleration(snapshot)
        )
        ordered = tuple(sorted(candidates, key=self._priority, reverse=True))
        selected = next((item for item in ordered if item.level is not RiskLevel.NORMAL), None)
        # Apply exit hysteresis to the active target even if a weaker new target ranks first.
        fsm_candidate = selected
        if self._fsm.active is not None:
            previous = next((item for item in ordered
                             if item.identity == self._fsm.active.identity), None)
            if selected is None:
                fsm_candidate = previous
            elif (previous is not None and selected.level.rank < self._fsm.level.rank
                  and previous.score > self._fsm.exit_score):
                fsm_candidate = previous
        stable, active, held = self._fsm.update(fsm_candidate, now_ns=now_ns)
        self._last_assessed_ns = now_ns
        self._sequence += 1
        header = Header.now(self.source_id, self._sequence, frame_id="vehicle",
                            capture_time_ns=now_ns, trace_id=snapshot.header.trace_id)
        return RiskAssessment(
            header=header, candidates=ordered, selected_risk=selected, active_risk=active,
            raw_level=selected.level if selected else RiskLevel.NORMAL, stable_level=stable,
            held=held,
            degraded=bool(snapshot.stale_sources or snapshot.degraded_reasons
                          or not snapshot.external or snapshot.vehicle is None
                          or snapshot.driver is None or not snapshot.driver.face_detected),
            unavailable_rules=tuple(dict.fromkeys(unavailable)),
            event=self._event(active, stable, header, now_ns),
        )

    def evaluate(
        self, snapshot: SituationSnapshot, *, now_ns: int | None = None
    ) -> RiskEvent | None:
        """Compatibility entry point for the existing DDS orchestration."""
        return self.assess(snapshot, now_ns=now_ns).event


__all__ = ["RiskEngine", "RiskPolicy", "load_risk_policy"]
