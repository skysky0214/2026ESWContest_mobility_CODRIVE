"""Live wire adapter to the team's candidate selection and four-state FSM."""

from dataclasses import fields, is_dataclass, replace
from enum import Enum
from . import models as wire
from .team_core.contracts import models as core
from .team_core.risk.engine import RiskEngine as TeamEngine
from .team_core.risk.policy import RiskPolicy as TeamPolicy, load_risk_policy as load_risk_policy
from .team_core.situation.fusion import FusionPolicy
from .live_risk_rules import LiveRules, RiskPolicy
from .environment_risk import environment_context


def convert(value, module):
    if isinstance(value, Enum):
        return getattr(module, type(value).__name__)(value.value)
    if is_dataclass(value):
        cls = getattr(module, type(value).__name__)
        return cls(
            **{
                f.name: convert(getattr(value, f.name), module)
                for f in fields(cls)
                if hasattr(value, f.name)
            }
        )
    if isinstance(value, tuple):
        return tuple(convert(v, module) for v in value)
    return value


class _LiveTeamEngine(TeamEngine):
    def _environment_context(self, snapshot):
        return self.live.environment_context

    def _driver_context(self, driver):
        live = self.live
        behavior = live.rules.driver_attention.active_behavior
        if driver is None or not driver.face_detected:
            return 1.0, None
        if behavior == "drowsy":
            return self.policy.drowsiness_weight, "driver_drowsiness"
        if behavior == "distracted":
            return self.policy.inattention_weight, "driver_inattention"
        if live.snapshot.driver.phone_detected:
            return self.policy.phone_weight, "driver_phone_use"
        return 1.0, None

    def _driver_and_audio(self, snapshot, driver_weight, driver_code):
        live = self.live
        candidates = (
            super()._driver_and_audio(replace(snapshot, driver=None), 1.0, None)
            if live.policy.audio_alerts_enabled
            else []
        )
        for item in live.rules._driver_and_audio(live.snapshot):
            if item.code == "emergency_vehicle":
                continue
            score = {
                wire.Severity.INFO: 35.0,
                wire.Severity.WARNING: 65.0,
                wire.Severity.CRITICAL: 100.0,
            }[item.severity]
            candidates.append(
                self._candidate(
                    item.code, "driver:cabin", score, item.action, item.message_key, item.evidence
                )
            )
        return candidates

    def _camera_contexts(self, snapshot):
        corridor = self.live.driving_corridor
        for perception in snapshot.external:
            vehicle = snapshot.vehicle
            if corridor is not None and corridor.motion_history:
                match = min(corridor.motion_history, key=lambda v:
                    abs(v.header.capture_time_ns-perception.header.capture_time_ns))
                if abs(match.header.capture_time_ns-perception.header.capture_time_ns) <= self.policy.max_context_skew_ms*1_000_000:
                    vehicle = convert(match, core)
            yield replace(snapshot, vehicle=vehicle, external=(perception,))

    def _road(self, snapshot, driver_weight, unavailable):
        if not snapshot.external:
            return super()._road(snapshot, driver_weight, unavailable)
        return [candidate for context in self._camera_contexts(snapshot)
                for candidate in super(_LiveTeamEngine, self)._road(context, driver_weight, unavailable)]

    def _objects(self, snapshot, driver_weight, unavailable):
        return [candidate for context in self._camera_contexts(snapshot)
                for candidate in super(_LiveTeamEngine, self)._objects(context, driver_weight, unavailable)]

    def _acceleration(self, snapshot):
        live = self.live
        if live.policy.full_pedal_warning_enabled:
            return [self._candidate(item.code,"vehicle:can",70.,item.action,item.message_key,item.evidence)
                    for item in live.rules._full_pedal(live.snapshot)]
        if not live.policy.can_speed_acceleration_enabled:
            return super()._acceleration(snapshot)
        return [
            self._candidate(
                item.code, "vehicle:can", 100.0, item.action, item.message_key, item.evidence
            )
            for item in live.rules._acceleration_mismatch(live.snapshot)
        ]


class RiskEngine:
    def __init__(self, source_id="pi5.risk", policy=None, *, vehicle_ttl_ms=120, external_ttl_ms=250, team_policy=None, driving_corridor=None):
        self.source_id = source_id
        self.policy = policy or RiskPolicy()
        self.vehicle_ttl_ms = vehicle_ttl_ms
        self.driving_corridor = driving_corridor
        self.rules = LiveRules(self.policy)
        self.driver_attention = self.rules.driver_attention
        self.speed_acceleration = self.rules.speed_acceleration
        self.core = _LiveTeamEngine(
            source_id=source_id,
            policy=team_policy or TeamPolicy(),
            freshness=FusionPolicy(vehicle_ttl_ms=vehicle_ttl_ms, external_ttl_ms=external_ttl_ms),
        )
        self.core.live = self
        self.assessment = None
        self.environment_context = (1.0, ())

    def evaluate(self, snapshot, *, now_ns=None):
        import time

        now_ns = time.time_ns() if now_ns is None else now_ns
        snapshot = replace(
            snapshot,
            driver=snapshot.driver
            if snapshot.driver
            and 0 <= now_ns - snapshot.driver.header.capture_time_ns <= 400_000_000
            else None,
            vehicle=snapshot.vehicle
            if snapshot.vehicle
            and snapshot.vehicle.can_valid
            and 0
            <= now_ns - snapshot.vehicle.header.capture_time_ns
            <= self.vehicle_ttl_ms * 1_000_000
            else None,
        )
        if self.driving_corridor is not None:
            snapshot = self.driving_corridor.filter(snapshot, now_ns=now_ns)
        self.environment_context = environment_context(
            snapshot.environment, now_ns=now_ns, policy=self.core.policy)
        self.snapshot = snapshot
        self.driver_attention.update(snapshot.driver, now_ns=now_ns)
        if self.policy.can_speed_acceleration_enabled:
            self.speed_acceleration.observe(snapshot.vehicle, now_ns)
        self.assessment = self.core.assess(convert(snapshot, core), now_ns=now_ns)
        return convert(self.assessment.event, wire) if self.assessment.event else None
