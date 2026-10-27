"""Raspberry Pi 5 fusion, risk, guidance, and HMI orchestration runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from threading import Event
from typing import Any, Protocol

from ...contracts.models import (
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
)
from ...guidance.policy import HmiPolicy
from ...monitoring.health import NodeHealthMonitor
from ...risk.engine import RiskEngine
from ...situation.fusion import SituationFusion
from ...transport.model_bus import JsonBus, decode_model, publish_model


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    snapshot: SituationSnapshot
    risk: RiskEvent | None
    hmi: HmiCommand | None
    rejected_samples: int


class SnapshotSink(Protocol):
    """Non-safety output updated after every fusion iteration."""

    def update_snapshot(
        self,
        snapshot: SituationSnapshot,
        *,
        observed_time_ns: int | None = None,
    ) -> None: ...


class PiOrchestrator:
    """One non-blocking 50 Hz fusion/risk/HMI iteration on Raspberry Pi 5."""

    INPUTS: tuple[tuple[str, type[Any]], ...] = (
        ("vehicle_state", VehicleState),
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
        snapshot_sink: SnapshotSink | None = None,
    ) -> None:
        self.bus = bus
        self.fusion = fusion or SituationFusion()
        self.risk_engine = risk_engine or RiskEngine()
        self.hmi_policy = hmi_policy or HmiPolicy()
        self.health_monitor = health_monitor or NodeHealthMonitor()
        self.snapshot_sink = snapshot_sink
        self.rejected_samples = 0

    def _receive_inputs(self, received_time_ns: int) -> None:
        for topic, model_type in self.INPUTS:
            for envelope in self.bus.take_json(topic, max_samples=128):
                try:
                    model = decode_model(envelope, model_type)
                    if not self.fusion.ingest(model, received_time_ns=received_time_ns):
                        self.rejected_samples += 1
                except (TypeError, ValueError):
                    self.rejected_samples += 1
        for envelope in self.bus.take_json("node_health", max_samples=128):
            try:
                health = decode_model(envelope, NodeHealth)
                if not self.health_monitor.ingest(health, received_time_ns=received_time_ns):
                    self.rejected_samples += 1
            except (TypeError, ValueError):
                self.rejected_samples += 1

    def step(self, *, now_ns: int | None = None) -> OrchestratorResult:
        now_ns = time.time_ns() if now_ns is None else now_ns
        self._receive_inputs(now_ns)
        snapshot = self.fusion.snapshot(now_ns=now_ns)
        missing_nodes = self.health_monitor.missing_nodes(now_ns=now_ns)
        if missing_nodes:
            snapshot = replace(
                snapshot,
                degraded_reasons=snapshot.degraded_reasons
                + tuple(f"heartbeat_missing:{node}" for node in missing_nodes),
            )
        publish_model(self.bus, "situation_snapshot", snapshot)
        if self.snapshot_sink is not None:
            self.snapshot_sink.update_snapshot(snapshot, observed_time_ns=now_ns)
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
                next_tick = time.monotonic()


__all__ = ["OrchestratorResult", "PiOrchestrator", "SnapshotSink"]
