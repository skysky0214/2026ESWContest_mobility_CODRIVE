"""On-demand AI Hub WAV loading and training augmentation."""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import numpy as np

from .archive import NestedZipArchive
from .features import LogMelConfig, decode_wav, fit_window, log_mel_spectrogram
from .manifest import AudioManifestRecord


class ArchiveAudioStore:
    """Lazily opens one archive per process, which is safe for DataLoader forks."""

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._archives: dict[str, NestedZipArchive] = {}

    def _reset_after_fork(self) -> None:
        if self._pid == os.getpid():
            return
        # Do not close inherited descriptors here: another process owns their
        # logical lifetime. Drop references and open independent readers.
        self._archives = {}
        self._pid = os.getpid()

    def read(self, record: AudioManifestRecord) -> bytes:
        self._reset_after_fork()
        path = str(Path(record.source_archive).expanduser().resolve())
        archive = self._archives.get(path)
        if archive is None:
            archive = NestedZipArchive(path).open()
            self._archives[path] = archive
        return archive.read(record.zip_member)

    def close(self) -> None:
        for archive in self._archives.values():
            archive.close()
        self._archives.clear()

    def __getstate__(self) -> dict[str, object]:
        return {"_pid": os.getpid(), "_archives": {}}

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)) + 1e-12))


def mix_at_snr(signal: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """Mix fixed-length waveforms with ``signal/noise == snr_db``."""

    signal = np.asarray(signal, dtype=np.float32)
    noise = np.asarray(noise, dtype=np.float32)
    if signal.shape != noise.shape:
        raise ValueError("signal and noise must have the same shape")
    noise_scale = _rms(signal) / (max(_rms(noise), 1e-6) * (10.0 ** (snr_db / 20.0)))
    mixed = signal + noise * noise_scale
    peak = float(np.max(np.abs(mixed)))
    if peak > 1.0:
        mixed = mixed / peak
    return mixed.astype(np.float32, copy=False)


class AudioTrainingDataset:
    """Minimal PyTorch-compatible dataset without importing torch at module load."""

    def __init__(
        self,
        records: Sequence[AudioManifestRecord],
        *,
        feature_config: LogMelConfig | None = None,
        augment: bool = False,
        seed: int = 2026,
        mix_probability: float = 0.55,
    ) -> None:
        if not records:
            raise ValueError("audio dataset is empty")
        if not 0.0 <= mix_probability <= 1.0:
            raise ValueError("mix_probability must be in [0, 1]")
        self.records = tuple(records)
        self.feature_config = feature_config or LogMelConfig()
        self.augment = augment
        self.seed = seed
        self.mix_probability = mix_probability
        self.store = ArchiveAudioStore()
        self._background_indices = tuple(
            index
            for index, record in enumerate(self.records)
            if not record.siren_target and not record.horn_target
        )

    def __len__(self) -> int:
        return len(self.records)

    def _rng(self, index: int) -> np.random.Generator:
        # Each worker has its own process id, while index/seed keep runs auditable.
        entropy = (self.seed, index, os.getpid())
        return np.random.default_rng(np.random.SeedSequence(entropy))

    def _waveform(self, record: AudioManifestRecord, rng: np.random.Generator) -> np.ndarray:
        waveform = decode_wav(
            self.store.read(record), target_sample_rate_hz=self.feature_config.sample_rate_hz
        )
        return fit_window(
            waveform,
            self.feature_config.window_samples,
            random_crop=self.augment,
            rng=rng,
        )

    def __getitem__(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        record = self.records[index]
        rng = self._rng(index)
        waveform = self._waveform(record, rng)
        is_positive = bool(record.siren_target or record.horn_target)
        if (
            self.augment
            and is_positive
            and self._background_indices
            and rng.random() < self.mix_probability
        ):
            noise_index = int(rng.choice(self._background_indices))
            noise = self._waveform(self.records[noise_index], rng)
            waveform = mix_at_snr(waveform, noise, float(rng.uniform(-8.0, 18.0)))
        if self.augment:
            gain = 10.0 ** (float(rng.uniform(-8.0, 5.0)) / 20.0)
            waveform = np.clip(waveform * gain, -1.0, 1.0).astype(np.float32)

        feature = log_mel_spectrogram(waveform, self.feature_config)
        if self.augment:
            feature = feature.copy()
            if rng.random() < 0.4:
                width = int(rng.integers(1, max(2, self.feature_config.n_mels // 10)))
                start = int(rng.integers(0, self.feature_config.n_mels - width + 1))
                feature[:, start : start + width, :] = 0.0
            if rng.random() < 0.4:
                width = int(rng.integers(1, max(2, self.feature_config.target_frames // 10)))
                start = int(rng.integers(0, self.feature_config.target_frames - width + 1))
                feature[:, :, start : start + width] = 0.0
        target = np.asarray((record.siren_target, record.horn_target), dtype=np.float32)
        return feature, target


def records_by_partition(
    records: Sequence[AudioManifestRecord],
) -> dict[str, list[AudioManifestRecord]]:
    grouped: dict[str, list[AudioManifestRecord]] = defaultdict(list)
    for record in records:
        grouped[record.partition].append(record)
    return dict(grouped)


__all__ = [
    "ArchiveAudioStore",
    "AudioTrainingDataset",
    "mix_at_snr",
    "records_by_partition",
]
