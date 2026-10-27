"""Adapter for yyj's trained log-mel CNN, retaining preprocessing and filtering.

Source: yyj commit 490e819, features/model/TemporalEventFilter implementation.
Unlike the waveform backend, this model uses independent siren/horn sigmoid
scores, checkpoint thresholds, EMA and confirmation/hysteresis.
"""

from __future__ import annotations

import time

import numpy as np

from ...models import AudioEvent
from .features import log_mel_spectrogram, pcm16le_to_float32
from .model import load_audio_backend
from .temporal import TemporalEventFilter


class LogMelClassifier:
    def __init__(
        self,
        model_path,
        *,
        device="cuda:0",
        window_ms=960,
        enable_horn=True,
        on_frames=2,
        off_frames=5,
    ):
        self.backend = load_audio_backend(model_path, device=device)
        self.config = self.backend.feature_config
        if self.config.sample_rate_hz != 16000 or self.config.window_ms != window_ms:
            raise ValueError("Checkpoint sample rate/window differs from 16kHz capture profile")
        if len(self.backend.thresholds) != 2:
            raise ValueError("Checkpoint requires siren/horn thresholds")
        self.event_filter = TemporalEventFilter(
            siren_on_threshold=float(self.backend.thresholds[0]),
            horn_on_threshold=float(self.backend.thresholds[1]),
            enable_horn=enable_horn,
            on_frames=on_frames,
            off_frames=off_frames,
        )
        self.last_probabilities = (0.0, 0.0)
        self.inference_ms = 0.0
        # Warm up before opening capture; do not feed this synthetic input into the filter.
        self.backend.predict_probabilities(
            np.zeros((1, self.config.n_mels, self.config.target_frames), dtype=np.float32)
        )

    def reset(self):
        self.event_filter.reset()
        self.last_probabilities = (0.0, 0.0)

    def classify(self, window, header):
        waveform = pcm16le_to_float32(window.pcm16)
        if len(waveform) != self.config.window_samples:
            raise ValueError("Audio PCM length differs from checkpoint window")
        started = time.perf_counter_ns()
        feature = log_mel_spectrogram(waveform, self.config)
        probabilities = self.backend.predict_probabilities(feature)
        if np.asarray(probabilities).shape != (2,) or not np.isfinite(probabilities).all():
            raise ValueError("Audio model must return finite siren/horn probabilities")
        kind, confidence = self.event_filter.update(probabilities)
        self.last_probabilities = self.event_filter.smoothed_probabilities
        self.inference_ms = (time.perf_counter_ns() - started) / 1_000_000
        return AudioEvent(header, kind, confidence, window.window_ms, self.backend.model_version)
