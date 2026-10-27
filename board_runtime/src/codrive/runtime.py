"""Node-level glue between domain models and the DDS transport.

Inference models, DBC decoding, and the GUI are injected at hardware boundaries;
the safety orchestration in this module is runnable with either Cyclone DDS or
the deterministic in-memory bus.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from threading import Event
from typing import Any, Protocol, TypeVar

from .fusion import SituationFusion
from .guidance import HmiPolicy
from .health import NodeHealthMonitor
from .models import (
    AudioEvent,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    HmiCommand,
    InsState,
    NodeHealth,
    RiskEvent,
    SituationSnapshot,
    VehicleState,
    TurnIndicatorState,
    ScenarioContext,
    from_json,
    to_json,
)
from .risk import RiskEngine
from .transport import DdsEnvelope


class JsonBus(Protocol):
    def publish_json(
        self,
        topic: str,
        payload: str,
        *,
        source_id: str,
        sequence: int,
        capture_time_ns: int | None = None,
        publish_time_ns: int | None = None,
        schema_version: int | None = None,
    ) -> DdsEnvelope: ...

    def take_json(self, topic: str, max_samples: int = 1) -> list[DdsEnvelope]: ...


class ModelEnvelopeError(ValueError):
    pass


Model = TypeVar("Model")


def publish_model(bus: JsonBus, topic: str, model: Any) -> DdsEnvelope:
    header = getattr(model, "header", None)
    if header is None:
        raise TypeError("published domain models must contain a header")
    return bus.publish_json(
        topic,
        to_json(model),
        source_id=header.source_id,
        sequence=header.sequence,
        capture_time_ns=header.capture_time_ns,
        publish_time_ns=header.publish_time_ns,
        schema_version=header.schema_version,
    )


def decode_model(envelope: DdsEnvelope, model_type: type[Model]) -> Model:
    model = from_json(model_type, envelope.payload_json)
    header = getattr(model, "header", None)
    if header is None:
        raise ModelEnvelopeError("decoded model has no header")
    comparisons = {
        "source_id": (header.source_id, envelope.source_id),
        "sequence": (header.sequence, envelope.sequence),
        "capture_time_ns": (header.capture_time_ns, envelope.capture_time_ns),
        "publish_time_ns": (header.publish_time_ns, envelope.publish_time_ns),
        "schema_version": (header.schema_version, envelope.schema_version),
    }
    mismatched = [name for name, (inner, outer) in comparisons.items() if inner != outer]
    if mismatched:
        raise ModelEnvelopeError(f"payload/envelope mismatch: {', '.join(mismatched)}")
    return model


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    snapshot: SituationSnapshot
    risk: RiskEvent | None
    hmi: HmiCommand | None
    rejected_samples: int


class PiOrchestrator:
    """One non-blocking 50 Hz fusion/risk/HMI iteration on Raspberry Pi 5."""

    INPUTS: tuple[tuple[str, type[Any]], ...] = (
        ("vehicle_state", VehicleState),
        ("turn_indicator_state", TurnIndicatorState),
        ("scenario_context", ScenarioContext),
        ("ins_state", InsState),
        ("external_perception", ExternalPerception),
        ("driver_state", DriverState),
        ("audio_event", AudioEvent),
        ("environment_state", EnvironmentState),
    )

    def __init__(
        self,
        bus: JsonBus,
        *,
        fusion: SituationFusion | None = None,
        risk_engine: RiskEngine | None = None,
        hmi_policy: HmiPolicy | None = None,
        health_monitor: NodeHealthMonitor | None = None,
    ) -> None:
        self.bus = bus
        self.fusion = fusion or SituationFusion()
        self.risk_engine = risk_engine or RiskEngine()
        self._base_audio_alerts = self.risk_engine.policy.audio_alerts_enabled
        self.hmi_policy = hmi_policy or HmiPolicy()
        self.health_monitor = health_monitor or NodeHealthMonitor()
        self.rejected_samples = 0
        self.received_samples: dict[str, int] = {}

    def _receive_inputs(self, received_time_ns: int) -> None:
        for topic, model_type in self.INPUTS:
            for envelope in self.bus.take_json(topic, max_samples=128):
                try:
                    model = decode_model(envelope, model_type)
                    if not self.fusion.ingest(model, received_time_ns=received_time_ns):
                        self.rejected_samples += 1
                    else:
                        self.received_samples[topic] = self.received_samples.get(topic, 0) + 1
                except (TypeError, ValueError):
                    self.rejected_samples += 1
        for envelope in self.bus.take_json("node_health", max_samples=128):
            try:
                health = decode_model(envelope, NodeHealth)
                if not self.health_monitor.ingest(health, received_time_ns=received_time_ns):
                    self.rejected_samples += 1
                else:
                    self.received_samples["node_health"] = self.received_samples.get("node_health", 0) + 1
            except (TypeError, ValueError):
                self.rejected_samples += 1

    def step(self, *, now_ns: int | None = None) -> OrchestratorResult:
        now_ns = time.time_ns() if now_ns is None else now_ns
        self._receive_inputs(now_ns)
        context = self.fusion.scenario_context(now_ns)
        enabled = self._base_audio_alerts or bool(context and context.audio_alerts)
        if self.risk_engine.policy.audio_alerts_enabled != enabled:
            self.risk_engine.policy = replace(self.risk_engine.policy, audio_alerts_enabled=enabled)
            self.risk_engine.rules.policy = self.risk_engine.policy
        snapshot = self.fusion.snapshot(now_ns=now_ns)
        missing_nodes = self.health_monitor.missing_nodes(now_ns=now_ns)
        if missing_nodes:
            snapshot = replace(
                snapshot,
                degraded_reasons=snapshot.degraded_reasons
                + tuple(f"heartbeat_missing:{node}" for node in missing_nodes),
            )
        publish_model(self.bus, "situation_snapshot", snapshot)
        risk = self.risk_engine.evaluate(snapshot, now_ns=now_ns)
        command = None
        if risk is not None:
            publish_model(self.bus, "risk_event", risk)
            command = self.hmi_policy.render(risk)
            publish_model(self.bus, "hmi_command", command)
        return OrchestratorResult(snapshot, risk, command, self.rejected_samples)

    def run(self, stop: Event, *, rate_hz: float = 50.0) -> None:
        if rate_hz <= 0:
            raise ValueError("rate_hz must be positive")
        period_s = 1.0 / rate_hz
        next_tick = time.monotonic()
        while not stop.is_set():
            self.step()
            next_tick += period_s
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                stop.wait(remaining)
            else:
                # Do not perform a burst of stale iterations after an overrun.
                next_tick = time.monotonic()


class JetsonPerceptionPublisher:
    """Strict publishing boundary used by either statically-sharded Jetson."""

    def __init__(self, bus: JsonBus, source_id: str) -> None:
        self.bus = bus
        self.source_id = source_id

    def _check_source(self, model: Any) -> None:
        model_source = model.header.source_id
        if model_source != self.source_id and not model_source.startswith(f"{self.source_id}."):
            raise ValueError(f"worker {self.source_id!r} cannot publish source {model_source!r}")

    def external(self, model: ExternalPerception) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "external_perception", model)

    def driver(self, model: DriverState) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "driver_state", model)

    def audio(self, model: AudioEvent) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "audio_event", model)
