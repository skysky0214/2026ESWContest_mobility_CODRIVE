"""Rolling-window inference and temporal event stabilization."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

from ...contracts.models import AudioEvent, AudioKind, Header
from .features import LogMelConfig, log_mel_spectrogram, pcm16le_to_float32


class AudioClassifierBackend(Protocol):
    model_version: str

    def predict_probabilities(self, feature: np.ndarray) -> Sequence[float]: ...


@dataclass(frozen=True, slots=True)
class AudioInferenceResult:
    event: AudioEvent
    siren_probability: float
    horn_probability: float
    inference_ms: float
    feature: np.ndarray
    waveform: np.ndarray


class TemporalEventFilter:
    """EMA plus separate enter/leave thresholds to suppress alert flicker."""

    def __init__(
        self,
        *,
        ema_alpha: float = 0.6,
        on_threshold: float = 0.72,
        siren_on_threshold: float | None = None,
        horn_on_threshold: float | None = None,
        off_threshold: float = 0.42,
        on_frames: int = 2,
        off_frames: int = 5,
        enable_horn: bool = True,
    ) -> None:
        if not 0.0 < ema_alpha <= 1.0:
            raise ValueError("ema_alpha must be in (0, 1]")
        siren_threshold = on_threshold if siren_on_threshold is None else siren_on_threshold
        horn_threshold = on_threshold if horn_on_threshold is None else horn_on_threshold
        if not (
            0.0 <= off_threshold < siren_threshold <= 1.0
            and 0.0 <= off_threshold < horn_threshold <= 1.0
        ):
            raise ValueError("thresholds must satisfy 0 <= off < each on <= 1")
        if on_frames < 1 or off_frames < 1:
            raise ValueError("frame counts must be positive")
        self.ema_alpha = ema_alpha
        self.on_threshold = on_threshold
        self.siren_on_threshold = siren_threshold
        self.horn_on_threshold = horn_threshold
        self.off_threshold = off_threshold
        self.on_frames = on_frames
        self.off_frames = off_frames
        self.enable_horn = enable_horn
        self._ema: np.ndarray | None = None
        self._active = AudioKind.NONE
        self._candidate = AudioKind.NONE
        self._candidate_frames = 0
        self._off_frames = 0

    @property
    def smoothed_probabilities(self) -> tuple[float, float]:
        if self._ema is None:
            return 0.0, 0.0
        return float(self._ema[0]), float(self._ema[1])

    def reset(self) -> None:
        self._ema = None
        self._active = AudioKind.NONE
        self._candidate = AudioKind.NONE
        self._candidate_frames = 0
        self._off_frames = 0

    def update(self, probabilities: Sequence[float]) -> tuple[AudioKind, float]:
        values = np.asarray(tuple(probabilities), dtype=np.float32)
        if values.shape != (2,) or not np.isfinite(values).all():
            raise ValueError("classifier must return finite [siren, horn] probabilities")
        values = np.clip(values, 0.0, 1.0)
        if self._ema is None:
            self._ema = values
        else:
            self._ema = self.ema_alpha * values + (1.0 - self.ema_alpha) * self._ema
        siren_probability, horn_probability = self.smoothed_probabilities

        # A siren can pre-empt a horn after the same confirmation period.
        proposed = (
            AudioKind.SIREN
            if siren_probability >= self.siren_on_threshold
            else AudioKind.HORN
            if self.enable_horn and horn_probability >= self.horn_on_threshold
            else AudioKind.NONE
        )
        if self._active is AudioKind.NONE or (
            self._active is AudioKind.HORN and proposed is AudioKind.SIREN
        ):
            if proposed is self._candidate and proposed is not AudioKind.NONE:
                self._candidate_frames += 1
            else:
                self._candidate = proposed
                self._candidate_frames = 1 if proposed is not AudioKind.NONE else 0
            if self._candidate_frames >= self.on_frames:
                self._active = self._candidate
                self._candidate_frames = 0
                self._off_frames = 0
        else:
            active_probability = (
                siren_probability if self._active is AudioKind.SIREN else horn_probability
            )
            if active_probability < self.off_threshold:
                self._off_frames += 1
                if self._off_frames >= self.off_frames:
                    self._active = AudioKind.NONE
                    self._candidate = AudioKind.NONE
                    self._candidate_frames = 0
                    self._off_frames = 0
            else:
                self._off_frames = 0

        if self._active is AudioKind.SIREN:
            return self._active, siren_probability
        if self._active is AudioKind.HORN:
            return self._active, horn_probability
        strongest_enabled = (
            max(siren_probability, horn_probability)
            if self.enable_horn
            else siren_probability
        )
        return AudioKind.NONE, max(0.0, 1.0 - strongest_enabled)


class _RollingBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._samples = np.zeros(capacity, dtype=np.float32)
        self._write_index = 0
        self._count = 0

    @property
    def full(self) -> bool:
        return self._count == self._samples.size

    def append(self, values: np.ndarray) -> None:
        incoming = np.asarray(values, dtype=np.float32).reshape(-1)
        if incoming.size >= self._samples.size:
            self._samples[:] = incoming[-self._samples.size :]
            self._write_index = 0
            self._count = self._samples.size
            return
        first = min(incoming.size, self._samples.size - self._write_index)
        self._samples[self._write_index : self._write_index + first] = incoming[:first]
        remainder = incoming.size - first
        if remainder:
            self._samples[:remainder] = incoming[first:]
        self._write_index = (self._write_index + incoming.size) % self._samples.size
        self._count = min(self._samples.size, self._count + incoming.size)

    def latest(self) -> np.ndarray:
        if not self.full:
            raise RuntimeError("rolling buffer is not full")
        if self._write_index == 0:
            return self._samples.copy()
        return np.concatenate(
            (self._samples[self._write_index :], self._samples[: self._write_index])
        )


class RollingAudioWorker:
    """Accept arbitrary PCM chunks and infer on a 960 ms window every 100 ms."""

    def __init__(
        self,
        backend: AudioClassifierBackend,
        *,
        source_id: str = "jetson-cabin.audio",
        frame_id: str = "microphone",
        feature_config: LogMelConfig | None = None,
        hop_ms: int = 100,
        event_filter: TemporalEventFilter | None = None,
    ) -> None:
        self.backend = backend
        self.source_id = source_id
        self.frame_id = frame_id
        self.feature_config = feature_config or LogMelConfig()
        if hop_ms <= 0:
            raise ValueError("hop_ms must be positive")
        self.hop_samples = round(self.feature_config.sample_rate_hz * hop_ms / 1_000)
        self.event_filter = event_filter or TemporalEventFilter()
        self._buffer = _RollingBuffer(self.feature_config.window_samples)
        self._total_samples = 0
        self._next_inference_sample = self.feature_config.window_samples
        self._sequence = 0

    def feed_pcm16(
        self,
        payload: bytes | bytearray | memoryview,
        *,
        sample_rate_hz: int = 16_000,
        capture_time_ns: int | None = None,
    ) -> tuple[AudioInferenceResult, ...]:
        if sample_rate_hz != self.feature_config.sample_rate_hz:
            raise ValueError(
                f"stream must be {self.feature_config.sample_rate_hz} Hz, got {sample_rate_hz}"
            )
        return self.feed_float(
            pcm16le_to_float32(payload), capture_time_ns=capture_time_ns
        )

    def feed_float(
        self, waveform: np.ndarray, *, capture_time_ns: int | None = None
    ) -> tuple[AudioInferenceResult, ...]:
        samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if not np.isfinite(samples).all():
            raise ValueError("waveform contains non-finite samples")
        end_time_ns = time.time_ns() if capture_time_ns is None else capture_time_ns
        cursor = 0
        results: list[AudioInferenceResult] = []
        while cursor < samples.size:
            until_inference = max(1, self._next_inference_sample - self._total_samples)
            amount = min(samples.size - cursor, until_inference)
            self._buffer.append(samples[cursor : cursor + amount])
            cursor += amount
            self._total_samples += amount
            if self._total_samples < self._next_inference_sample:
                continue
            trailing_samples = samples.size - cursor
            inference_capture_ns = end_time_ns - round(
                trailing_samples * 1_000_000_000 / self.feature_config.sample_rate_hz
            )
            results.append(self._infer(inference_capture_ns))
            self._next_inference_sample += self.hop_samples
        return tuple(results)

    def _infer(self, capture_time_ns: int) -> AudioInferenceResult:
        waveform = self._buffer.latest()
        feature = log_mel_spectrogram(waveform, self.feature_config)
        started_ns = time.perf_counter_ns()
        raw_probabilities = self.backend.predict_probabilities(feature)
        inference_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
        kind, confidence = self.event_filter.update(raw_probabilities)
        siren_probability, horn_probability = self.event_filter.smoothed_probabilities
        self._sequence += 1
        event = AudioEvent(
            header=Header.now(
                self.source_id,
                self._sequence,
                frame_id=self.frame_id,
                capture_time_ns=capture_time_ns,
            ),
            kind=kind,
            confidence=confidence,
            window_ms=self.feature_config.window_ms,
            model_version=self.backend.model_version,
            direction="unknown",
        )
        return AudioInferenceResult(
            event=event,
            siren_probability=siren_probability,
            horn_probability=horn_probability,
            inference_ms=inference_ms,
            feature=feature,
            waveform=waveform,
        )


__all__ = [
    "AudioClassifierBackend",
    "AudioInferenceResult",
    "RollingAudioWorker",
    "TemporalEventFilter",
]
