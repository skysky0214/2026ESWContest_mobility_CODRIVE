"""State stabilization independent of sensors, transport, and warning delivery."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.models import RiskCandidate, RiskLevel


@dataclass(slots=True)
class RiskStateMachine:
    caution_exit_score: float = 20.0
    warning_exit_score: float = 45.0
    emergency_exit_score: float = 75.0
    release_hold_ms: int = 500
    state_gap_reset_ms: int = 1_000
    level: RiskLevel = RiskLevel.NORMAL
    active: RiskCandidate | None = None
    _release_since_ns: int | None = None
    _last_update_ns: int | None = None

    @property
    def exit_score(self) -> float:
        return {
            RiskLevel.NORMAL: 0.0,
            RiskLevel.CAUTION: self.caution_exit_score,
            RiskLevel.WARNING: self.warning_exit_score,
            RiskLevel.EMERGENCY: self.emergency_exit_score,
        }[self.level]

    def _accept(
        self, candidate: RiskCandidate | None
    ) -> tuple[RiskLevel, RiskCandidate | None, bool]:
        if candidate is not None and candidate.level is RiskLevel.NORMAL:
            candidate = None
        self.active = candidate
        self.level = candidate.level if candidate else RiskLevel.NORMAL
        self._release_since_ns = None
        return self.level, self.active, False

    def update(
        self, candidate: RiskCandidate | None, *, now_ns: int
    ) -> tuple[RiskLevel, RiskCandidate | None, bool]:
        if self._last_update_ns is not None and now_ns < self._last_update_ns:
            raise ValueError("FSM time must not move backwards")
        discontinuity = (self._last_update_ns is not None
                         and now_ns - self._last_update_ns > self.state_gap_reset_ms * 1_000_000)
        self._last_update_ns = now_ns
        if discontinuity:
            return self._accept(candidate)
        raw = candidate.level if candidate else RiskLevel.NORMAL
        if self.active is None or raw.rank >= self.level.rank:
            return self._accept(candidate)

        # Hysteresis belongs to the same hazard, never to an unrelated new target.
        same_target = candidate is not None and candidate.identity == self.active.identity
        if same_target and candidate.score > self.exit_score:
            self._release_since_ns = None
            return self.level, self.active, True
        if self._release_since_ns is None:
            self._release_since_ns = now_ns
        if now_ns - self._release_since_ns >= self.release_hold_ms * 1_000_000:
            return self._accept(candidate)
        return self.level, self.active, True
