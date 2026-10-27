"""Time-aware fusion of the latest edge perception and vehicle state."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..contracts.models import (
    AudioEvent,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    InsState,
    SituationSnapshot,
    VehicleState,
)


@dataclass(frozen=True, slots=True)
class FusionPolicy:
    vehicle_ttl_ms: int = 120
    ins_ttl_ms: int = 250
    external_ttl_ms: int = 250
    driver_ttl_ms: int = 400
    audio_ttl_ms: int = 1_000
    environment_ttl_ms: int = 300_000
    max_future_skew_ms: int = 50
    history_depth: int = 32
    expected_camera_ids: tuple[str, ...] = ("front",)
    # Pairs of (stream id, allowed source). Repeat a stream to permit hot spares.
    source_allowlist: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("vehicle_ttl_ms", "ins_ttl_ms", "external_ttl_ms", "driver_ttl_ms",
                     "audio_ttl_ms", "environment_ttl_ms", "max_future_skew_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (isinstance(self.history_depth, bool) or not isinstance(self.history_depth, int)
                or self.history_depth < 1):
            raise ValueError("history_depth must be a positive integer")


@dataclass(slots=True)
class _Sample:
    value: Any
    received_time_ns: int


@dataclass(slots=True)
class SituationFusion:
    source_id: str = "pi5.fusion"
    policy: FusionPolicy = field(default_factory=FusionPolicy)
    _sequence: int = 0
    _history: dict[str, deque[_Sample]] = field(default_factory=dict)
    _last_sequence: dict[tuple[str, str], tuple[str, int, int]] = field(default_factory=dict)

    @staticmethod
    def _stream_id(sample: Any) -> str:
        if isinstance(sample, ExternalPerception):
            return f"external:{sample.camera_id}"
        if isinstance(sample, DriverState):
            return f"driver:{sample.camera_id}"
        if isinstance(sample, AudioEvent):
            return "audio"
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
            if same_boot and header.capture_time_ns < previous_capture_ns:
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
        self._history.setdefault(stream_id, deque(maxlen=self.policy.history_depth)).append(wrapped)
        return True

    @staticmethod
    def _fresh(samples: deque[_Sample] | None, now_ns: int, ttl_ms: int) -> Any | None:
        eligible = (
            sample.value for sample in (samples or ())
            if 0 <= now_ns - sample.value.header.capture_time_ns <= ttl_ms * 1_000_000
        )
        # A slightly future-dated sample must not hide the previous usable sample.
        return max(eligible, key=lambda value: value.header.capture_time_ns, default=None)

    def _latest(self, kind: str, now_ns: int, ttl_ms: int) -> Any | None:
        values = (
            value for stream, history in self._history.items()
            if stream == kind or stream.startswith(f"{kind}:")
            if (value := self._fresh(history, now_ns, ttl_ms)) is not None
        )
        return max(values, key=lambda value: value.header.capture_time_ns, default=None)

    def snapshot(self, *, now_ns: int | None = None) -> SituationSnapshot:
        now_ns = time.time_ns() if now_ns is None else now_ns
        vehicle = self._latest("vehicle", now_ns, self.policy.vehicle_ttl_ms)
        ins = self._latest("ins", now_ns, self.policy.ins_ttl_ms)
        driver = self._latest("driver", now_ns, self.policy.driver_ttl_ms)
        audio = self._latest("audio", now_ns, self.policy.audio_ttl_ms)
        environment = self._latest("environment", now_ns, self.policy.environment_ttl_ms)
        external = tuple(
            value
            for stream in sorted(self._history)
            if stream.startswith("external:")
            if (
                value := self._fresh(self._history[stream], now_ns, self.policy.external_ttl_ms)
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
