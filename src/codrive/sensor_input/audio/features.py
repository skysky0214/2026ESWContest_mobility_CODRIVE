"""Dependency-light PCM/WAV preprocessing shared by training and deployment."""

from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

import numpy as np


@dataclass(frozen=True, slots=True)
class LogMelConfig:
    sample_rate_hz: int = 16_000
    window_ms: int = 960
    frame_ms: float = 25.0
    frame_hop_ms: float = 10.0
    n_fft: int = 512
    n_mels: int = 64
    target_frames: int = 96
    f_min_hz: float = 50.0
    f_max_hz: float = 8_000.0
    log_floor: float = 1e-10

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0 or self.window_ms <= 0:
            raise ValueError("sample rate and window must be positive")
        if self.frame_ms <= 0 or self.frame_hop_ms <= 0:
            raise ValueError("STFT frame and hop must be positive")
        if self.n_fft <= 0 or self.n_mels <= 0 or self.target_frames <= 0:
            raise ValueError("FFT, mel, and target frame counts must be positive")
        if not 0.0 <= self.f_min_hz < self.f_max_hz <= self.sample_rate_hz / 2:
            raise ValueError("mel frequency bounds must lie inside Nyquist")

    @property
    def window_samples(self) -> int:
        return round(self.sample_rate_hz * self.window_ms / 1_000)

    @property
    def frame_samples(self) -> int:
        return round(self.sample_rate_hz * self.frame_ms / 1_000)

    @property
    def frame_hop_samples(self) -> int:
        return round(self.sample_rate_hz * self.frame_hop_ms / 1_000)


def pcm16le_to_float32(payload: bytes | bytearray | memoryview) -> np.ndarray:
    data = memoryview(payload)
    if len(data) % 2:
        raise ValueError("PCM16 payload length must be even")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16le(waveform: np.ndarray) -> bytes:
    samples = np.asarray(waveform, dtype=np.float32)
    clipped = np.clip(samples, -1.0, 1.0)
    return np.rint(clipped * 32767.0).astype("<i2").tobytes()


def resample_linear(waveform: np.ndarray, source_rate_hz: int, target_rate_hz: int) -> np.ndarray:
    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if source_rate_hz <= 0 or target_rate_hz <= 0:
        raise ValueError("sample rates must be positive")
    if source_rate_hz == target_rate_hz or samples.size == 0:
        return samples.copy()
    target_length = max(1, round(samples.size * target_rate_hz / source_rate_hz))
    source_positions = np.arange(samples.size, dtype=np.float64)
    target_positions = np.arange(target_length, dtype=np.float64) * (
        source_rate_hz / target_rate_hz
    )
    target_positions = np.minimum(target_positions, samples.size - 1)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def _decode_frames(raw: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")


def decode_wav(
    source: bytes | bytearray | memoryview | str | Path | BinaryIO,
    *,
    target_sample_rate_hz: int = 16_000,
) -> np.ndarray:
    """Decode uncompressed PCM WAV as normalized mono float32."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        handle: BinaryIO = io.BytesIO(bytes(source))
        should_close = True
    elif isinstance(source, (str, Path)):
        handle = Path(source).expanduser().open("rb")
        should_close = True
    else:
        handle = source
        should_close = False

    try:
        with wave.open(handle, "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise ValueError(f"compressed WAV is unsupported: {wav.getcomptype()}")
            channels = wav.getnchannels()
            if channels <= 0:
                raise ValueError("WAV has no channels")
            source_rate_hz = wav.getframerate()
            samples = _decode_frames(wav.readframes(wav.getnframes()), wav.getsampwidth())
        samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
        return resample_linear(samples, source_rate_hz, target_sample_rate_hz)
    finally:
        if should_close:
            handle.close()


def fit_window(
    waveform: np.ndarray,
    sample_count: int,
    *,
    random_crop: bool = False,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    if samples.size == sample_count:
        return samples.copy()
    if samples.size > sample_count:
        maximum_start = samples.size - sample_count
        if random_crop:
            generator = rng or np.random.default_rng()
            start = int(generator.integers(0, maximum_start + 1))
        else:
            start = maximum_start // 2
        return samples[start : start + sample_count].copy()
    missing = sample_count - samples.size
    left = missing // 2
    right = missing - left
    return np.pad(samples, (left, right), mode="constant").astype(np.float32, copy=False)


def _hz_to_mel(frequency_hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(frequency_hz) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (np.power(10.0, np.asarray(mel) / 2595.0) - 1.0)


@lru_cache(maxsize=16)
def _mel_filter_bank(
    sample_rate_hz: int,
    n_fft: int,
    n_mels: int,
    f_min_hz: float,
    f_max_hz: float,
) -> np.ndarray:
    mel_edges = np.linspace(_hz_to_mel(f_min_hz), _hz_to_mel(f_max_hz), n_mels + 2)
    hz_edges = _mel_to_hz(mel_edges)
    fft_frequencies = np.linspace(0.0, sample_rate_hz / 2.0, n_fft // 2 + 1)
    filters = np.zeros((n_mels, fft_frequencies.size), dtype=np.float32)
    for index in range(n_mels):
        left, center, right = hz_edges[index : index + 3]
        rising = (fft_frequencies - left) / max(center - left, np.finfo(np.float32).eps)
        falling = (right - fft_frequencies) / max(right - center, np.finfo(np.float32).eps)
        filters[index] = np.maximum(0.0, np.minimum(rising, falling))
    return filters


def log_mel_spectrogram(waveform: np.ndarray, config: LogMelConfig | None = None) -> np.ndarray:
    """Return one normalized ``[1, n_mels, target_frames]`` feature tensor."""

    config = config or LogMelConfig()
    samples = fit_window(waveform, config.window_samples)
    frame_size = config.frame_samples
    hop_size = config.frame_hop_samples
    if frame_size > samples.size:
        raise ValueError("STFT frame is longer than the waveform window")
    frame_starts = np.arange(0, samples.size - frame_size + 1, hop_size, dtype=np.int64)
    frames = np.stack([samples[start : start + frame_size] for start in frame_starts])
    frames *= np.hanning(frame_size).astype(np.float32)
    spectrum = np.fft.rfft(frames, n=config.n_fft, axis=1)
    power = np.square(np.abs(spectrum), dtype=np.float64).astype(np.float32)
    filters = _mel_filter_bank(
        config.sample_rate_hz,
        config.n_fft,
        config.n_mels,
        config.f_min_hz,
        config.f_max_hz,
    )
    feature = np.log(np.maximum(filters @ power.T, config.log_floor)).astype(np.float32)

    frame_delta = config.target_frames - feature.shape[1]
    if frame_delta > 0:
        left = frame_delta // 2
        feature = np.pad(feature, ((0, 0), (left, frame_delta - left)), mode="edge")
    elif frame_delta < 0:
        start = (-frame_delta) // 2
        feature = feature[:, start : start + config.target_frames]

    mean = float(feature.mean())
    standard_deviation = float(feature.std())
    feature = (feature - mean) / max(standard_deviation, 1e-6)
    if not np.isfinite(feature).all():
        raise ValueError("log-mel feature contains a non-finite value")
    return feature[np.newaxis, ...].astype(np.float32, copy=False)


def feature_to_mono8(feature: np.ndarray) -> np.ndarray:
    """Convert a log-mel tensor to a frequency-up image for RViz."""

    values = np.asarray(feature, dtype=np.float32).squeeze()
    if values.ndim != 2:
        raise ValueError("feature must reduce to [mel, time]")
    low, high = np.percentile(values, (2.0, 98.0))
    normalized = np.clip((values - low) / max(float(high - low), 1e-6), 0.0, 1.0)
    return np.flipud(np.rint(normalized * 255.0).astype(np.uint8))


__all__ = [
    "LogMelConfig",
    "decode_wav",
    "feature_to_mono8",
    "fit_window",
    "float32_to_pcm16le",
    "log_mel_spectrogram",
    "pcm16le_to_float32",
    "resample_linear",
]
