"""Explainable rules using signals represented in the XGBoost input contract.

Object ranging/zones, lanes, indicators and INS acceleration are excluded.
Confidence filtering remains the responsibility of the sensor producers.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field, replace

from ..situation.fusion import FusionPolicy
from ..contracts.models import (
    AudioKind, DriverState, Header, RiskAssessment, RiskCandidate, RiskEvent,
    RiskLevel, SituationSnapshot, TrafficLight,
)
from .fsm import RiskStateMachine
from .policy import RiskPolicy, load_risk_policy


@dataclass(slots=True)
class RiskEngine:
    source_id: str = "pi5.risk"
    policy: RiskPolicy = field(default_factory=RiskPolicy)
    freshness: FusionPolicy = field(default_factory=FusionPolicy)
    _sequence: int = 0
    _fsm: RiskStateMachine = field(init=False)
    _last_emitted_ns: dict[tuple[str, str], int] = field(default_factory=dict)
    _last_active_identity: tuple[str, str] | None = None
    _last_active_level: RiskLevel = RiskLevel.NORMAL
    _eyes_closed_since_ns: int | None = None
    _driver_identity: tuple[str, str, str] | None = None
    _last_driver_capture_ns: int | None = None
    _last_assessed_ns: int | None = None

    def __post_init__(self) -> None:
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
        # INS is no longer required by any risk rule.
        stale = [source for source in snapshot.stale_sources if source != "ins"]

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

    def _candidate(self, code: str, target: str, score: float, action: str,
                   message_key: str, evidence: tuple[str, ...], **context) -> RiskCandidate:
        return RiskCandidate(
            code=code, target_id=target, level=self.level_for_score(score), score=score,
            recommended_action=action, message_key=message_key, evidence=evidence, **context,
        )

    def _road(self, snapshot: SituationSnapshot, driver_weight: float,
              unavailable: list[str]) -> list[RiskCandidate]:
        candidates = []
        vehicle = snapshot.vehicle
        if vehicle is None:
            unavailable.append("signal:vehicle_missing")
            return candidates
        for perception in snapshot.external:
            if perception.camera_id not in self.policy.road_camera_ids:
                continue
            if not self._aligned(vehicle, perception):
                unavailable.append(f"road:time_skew:{perception.camera_id}")
                continue
            if vehicle.speed_mps < self.policy.minimum_motion_mps:
                continue
            weight = driver_weight if self._aligned(snapshot.driver, vehicle, perception) else 1.0
            target = f"{perception.header.source_id}:{perception.camera_id}"
            if not vehicle.brake_pressed:
                if perception.traffic_light is TrafficLight.RED:
                    score = self.policy.red_light_base_score + (
                        self.policy.red_light_speed_bonus if vehicle.speed_mps >
                        self.policy.red_light_high_speed_mps else 0.0
                    )
                    candidates.append(self._candidate(
                        "red_light_approach", target, score * weight, "brake_now",
                        "red_light_brake", (f"speed_mps={vehicle.speed_mps:.2f}", "brake=false"),
                        direction="front", base_score=score, driver_weight=weight,
                    ))
                elif (perception.traffic_light is TrafficLight.YELLOW
                      and vehicle.speed_mps > self.policy.yellow_light_high_speed_mps):
                    candidates.append(self._candidate(
                        "yellow_light_approach", target,
                        self.policy.yellow_light_base_score * weight, "slow_down",
                        "yellow_light_slow", (f"speed_mps={vehicle.speed_mps:.2f}",),
                        direction="front", base_score=self.policy.yellow_light_base_score,
                        driver_weight=weight,
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
        if audio is not None:
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
                    (), direction=audio.direction,
                    base_score=score,
                ))
        return candidates

    def _priority(self, candidate: RiskCandidate) -> tuple:
        priorities = {"red_light_approach": 3, "yellow_light_approach": 2}
        return (candidate.level.rank, candidate.score,
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
        weight, driver_code = self._driver_context(snapshot.driver)
        candidates = (
            self._road(snapshot, weight, unavailable)
            + self._driver_and_audio(snapshot, weight, driver_code)
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
