"""Time-aware fusion of the latest edge perception and vehicle state."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from . import latency_probe as latency
from .models import (
    AudioEvent,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    InsState,
    SituationSnapshot,
    VehicleState,
    TurnIndicatorState,
    ScenarioContext,
    TurnSignal,
)


@dataclass(frozen=True, slots=True)
class FusionPolicy:
    allow_scenario_context: bool = True
    vehicle_ttl_ms: int = 120
    ins_ttl_ms: int = 250
    external_ttl_ms: int = 250
    driver_ttl_ms: int = 400
    audio_ttl_ms: int = 1_000
    environment_ttl_ms: int = 300_000
    max_future_skew_ms: int = 50
    expected_camera_ids: tuple[str, ...] = ("front",)
    # Pairs of (stream id, allowed source). Repeat a stream to permit hot spares.
    source_allowlist: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class _Sample:
    value: Any
    received_time_ns: int


@dataclass(slots=True)
class SituationFusion:
    source_id: str = "pi5.fusion"
    policy: FusionPolicy = field(default_factory=FusionPolicy)
    _sequence: int = 0
    _vehicle: _Sample | None = None
    _indicator: _Sample | None = None
    _scenario: _Sample | None = None
    _ins: _Sample | None = None
    _driver: _Sample | None = None
    _audio: _Sample | None = None
    _environment: _Sample | None = None
    _external: dict[str, _Sample] = field(default_factory=dict)
    _last_sequence: dict[tuple[str, str], tuple[str, int, int]] = field(default_factory=dict)

    @staticmethod
    def _stream_id(sample: Any) -> str:
        if isinstance(sample, ExternalPerception):
            return f"external:{sample.camera_id}"
        if isinstance(sample, DriverState):
            return f"driver:{sample.camera_id}"
        if isinstance(sample, AudioEvent):
            return "audio"
        if isinstance(sample, ScenarioContext):
            return "scenario_context"
        if isinstance(sample, TurnIndicatorState):
            return "turn_indicator"
        if isinstance(sample, VehicleState):
            return "vehicle"
        if isinstance(sample, InsState):
            return "ins"
        if isinstance(sample, EnvironmentState):
            return "environment"
        raise TypeError(f"unsupported fusion sample: {type(sample).__name__}")

    def ingest(self, sample: Any, *, received_time_ns: int | None = None) -> bool:
        """Store a sample if it is newer than the last sample from its source.

        Returns ``False`` for a duplicate/out-of-order sequence or a timestamp too
        far in the future. This prevents a restarted or unhealthy worker from
        poisoning the Pi's latest-state cache.
        """

        now_ns = time.time_ns() if received_time_ns is None else received_time_ns
        header = getattr(sample, "header", None)
        if not isinstance(header, Header):
            raise TypeError("fusion samples must contain a Header")
        if header.capture_time_ns > now_ns + self.policy.max_future_skew_ms * 1_000_000:
            return False
        stream_id = self._stream_id(sample)
        if isinstance(sample, ScenarioContext) and (not self.policy.allow_scenario_context or header.source_id != "laptop.carla.scenario"):
            return False
        allowed_sources = {
            source
            for configured_stream, source in self.policy.source_allowlist
            if configured_stream == stream_id
        }
        if allowed_sources and header.source_id not in allowed_sources:
            return False
        sequence_key = (header.source_id, stream_id)
        previous = self._last_sequence.get(sequence_key)
        if previous is not None:
            previous_boot, previous_sequence, previous_capture_ns = previous
            same_boot = header.boot_id == previous_boot
            if same_boot and header.sequence <= previous_sequence:
                return False
            # A restarted process may reset its sequence, but an old boot's late
            # packet must not replace newer state.
            if not same_boot and header.capture_time_ns < previous_capture_ns:
                return False
        self._last_sequence[sequence_key] = (
            header.boot_id,
            header.sequence,
            header.capture_time_ns,
        )
        wrapped = _Sample(sample, now_ns)
        if isinstance(sample, VehicleState):
            self._vehicle = wrapped
        elif isinstance(sample, ScenarioContext):
            self._scenario = wrapped
        elif isinstance(sample, TurnIndicatorState):
            self._indicator = wrapped
        elif isinstance(sample, InsState):
            self._ins = wrapped
        elif isinstance(sample, DriverState):
            self._driver = wrapped
        elif isinstance(sample, AudioEvent):
            self._audio = wrapped
        elif isinstance(sample, EnvironmentState):
            self._environment = wrapped
        elif isinstance(sample, ExternalPerception):
            self._external[sample.camera_id] = wrapped
        if latency.enabled():
            latency.record('sensor_receive', {'sample':latency.header_key(header),
                           'source':header.source_id,'capture_ns':header.capture_time_ns,
                           'publish_field_ns':header.publish_time_ns})
        return True

    @staticmethod
    def _fresh(sample: _Sample | None, now_ns: int, ttl_ms: int) -> Any | None:
        if sample is None:
            return None
        captured_ns = sample.value.header.capture_time_ns
        if captured_ns > now_ns or now_ns - captured_ns > ttl_ms * 1_000_000:
            return None
        return sample.value

    def scenario_context(self, now_ns):
        return self._fresh(self._scenario, now_ns, 2000)

    def snapshot(self, *, now_ns: int | None = None) -> SituationSnapshot:
        now_ns = time.time_ns() if now_ns is None else now_ns
        vehicle = self._fresh(self._vehicle, now_ns, self.policy.vehicle_ttl_ms)
        if vehicle is not None and not vehicle.can_valid:
            vehicle = None
        indicator = self._fresh(self._indicator, now_ns, 500)
        if vehicle is not None and self._indicator is not None:
            # No synthetic CAN packet: overlay only a separately received switch intent.
            intent = indicator.turn_signal if indicator and indicator.connected else TurnSignal.OFF
            vehicle = replace(vehicle, turn_signal=intent)
        ins = self._fresh(self._ins, now_ns, self.policy.ins_ttl_ms)
        driver = self._fresh(self._driver, now_ns, self.policy.driver_ttl_ms)
        audio = self._fresh(self._audio, now_ns, self.policy.audio_ttl_ms)
        environment = self._fresh(self._environment, now_ns, self.policy.environment_ttl_ms)
        context = self.scenario_context(now_ns)
        if context and context.environment is not None:
            # Only the explicitly selected, live demo may override real API weather.
            environment = self._fresh(_Sample(context.environment, now_ns), now_ns, 2000) or environment
        external = tuple(
            value
            for camera_id in sorted(self._external)
            if (
                value := self._fresh(self._external[camera_id], now_ns, self.policy.external_ttl_ms)
            )
            is not None
        )

        stale: list[str] = []
        if vehicle is None:
            stale.append("vehicle")
        if ins is None:
            stale.append("ins")
        if driver is None:
            stale.append("driver")
        fresh_cameras = {sample.camera_id for sample in external}
        stale.extend(
            f"camera:{camera_id}"
            for camera_id in self.policy.expected_camera_ids
            if camera_id not in fresh_cameras
        )

        degraded = [sample.degraded_reason for sample in external if sample.degraded_reason]
        if driver is not None and not driver.face_detected:
            degraded.append("driver:face_missing")
        self._sequence += 1
        return SituationSnapshot(
            header=Header.now(
                self.source_id,
                self._sequence,
                frame_id="vehicle",
                capture_time_ns=now_ns,
            ),
            vehicle=vehicle,
            ins=ins,
            external=external,
            driver=driver,
            audio=audio,
            environment=environment,
            stale_sources=tuple(stale),
            degraded_reasons=tuple(degraded),
        )
