from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ...models import AudioKind


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
            max(siren_probability, horn_probability) if self.enable_horn else siren_probability
        )
        return AudioKind.NONE, max(0.0, 1.0 - strongest_enabled)
