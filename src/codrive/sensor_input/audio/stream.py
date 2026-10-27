"""Bridge STM32 USART frames to the rolling inference worker."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ...hardware.audio.stm32 import AudioStreamDecoder, encode_audio_frame
from .features import float32_to_pcm16le
from .worker import AudioInferenceResult, RollingAudioWorker


@dataclass(frozen=True, slots=True)
class AudioSerialStats:
    decoded_frames: int
    sequence_gaps: int
    crc_errors: int
    invalid_headers: int
    bytes_discarded: int


class Stm32AudioInferenceAdapter:
    """Decode arbitrary USART chunks and preserve the STM32 audio timeline."""

    def __init__(self, worker: RollingAudioWorker) -> None:
        self.worker = worker
        self.decoder = AudioStreamDecoder()
        self._clock_offset_ns: int | None = None
        self._last_sequence: int | None = None
        self._decoded_frames = 0
        self._sequence_gaps = 0

    @property
    def stats(self) -> AudioSerialStats:
        return AudioSerialStats(
            decoded_frames=self._decoded_frames,
            sequence_gaps=self._sequence_gaps,
            crc_errors=self.decoder.crc_errors,
            invalid_headers=self.decoder.invalid_headers,
            bytes_discarded=self.decoder.bytes_discarded,
        )

    def feed_serial(
        self,
        data: bytes | bytearray | memoryview,
        *,
        received_time_ns: int | None = None,
    ) -> tuple[AudioInferenceResult, ...]:
        received_ns = time.time_ns() if received_time_ns is None else received_time_ns
        results: list[AudioInferenceResult] = []
        for frame in self.decoder.feed(data):
            if self._last_sequence is not None:
                expected = (self._last_sequence + 1) & 0xFFFFFFFF
                if frame.sequence != expected:
                    gap = (frame.sequence - expected) & 0xFFFFFFFF
                    self._sequence_gaps += max(1, gap)
            self._last_sequence = frame.sequence
            self._decoded_frames += 1
            frame_time_ns = frame.timestamp_us * 1_000
            if self._clock_offset_ns is None:
                self._clock_offset_ns = received_ns - frame_time_ns
            capture_time_ns = min(received_ns, self._clock_offset_ns + frame_time_ns)
            results.extend(
                self.worker.feed_pcm16(
                    frame.pcm16,
                    sample_rate_hz=frame.sample_rate_hz,
                    capture_time_ns=capture_time_ns,
                )
            )
        return tuple(results)


def encode_waveform_as_stm32_frames(
    waveform: np.ndarray,
    *,
    sample_rate_hz: int = 16_000,
    frame_ms: int = 20,
    first_sequence: int = 0,
    first_timestamp_us: int = 0,
) -> tuple[bytes, ...]:
    """Encode a WAV-derived signal exactly like the future microphone firmware."""

    if sample_rate_hz != 16_000:
        raise ValueError("STM32 protocol v1 requires 16 kHz")
    if frame_ms <= 0:
        raise ValueError("frame_ms must be positive")
    frame_samples = round(sample_rate_hz * frame_ms / 1_000)
    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    frames: list[bytes] = []
    for frame_index, start in enumerate(range(0, samples.size, frame_samples)):
        chunk = samples[start : start + frame_samples]
        if chunk.size < frame_samples:
            chunk = np.pad(chunk, (0, frame_samples - chunk.size), mode="constant")
        frames.append(
            encode_audio_frame(
                float32_to_pcm16le(chunk),
                sequence=(first_sequence + frame_index) & 0xFFFFFFFF,
                timestamp_us=first_timestamp_us + frame_index * frame_ms * 1_000,
            )
        )
    return tuple(frames)


__all__ = [
    "AudioSerialStats",
    "Stm32AudioInferenceAdapter",
    "encode_waveform_as_stm32_frames",
]
