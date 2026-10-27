"""Validated tuning parameters for the local rule engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    road_camera_ids: tuple[str, ...] = ("front",)
    red_light_base_score: float = 50.0
    red_light_speed_bonus: float = 20.0
    yellow_light_base_score: float = 30.0
    driver_inattention_score: float = 60.0
    driver_phone_score: float = 60.0
    driver_drowsiness_score: float = 85.0
    siren_base_score: float = 35.0
    horn_base_score: float = 30.0
    audio_speed_bonus: float = 15.0
    audio_direction_bonus: float = 15.0
    caution_score: float = 30.0
    warning_score: float = 60.0
    emergency_score: float = 90.0
    caution_exit_score: float = 20.0
    warning_exit_score: float = 45.0
    emergency_exit_score: float = 75.0
    minimum_motion_mps: float = 1.4
    high_speed_mps: float = 40 / 3.6
    distraction_threshold: float = 0.35
    drowsiness_threshold: float = 0.7
    eyes_closed_hold_ms: int = 800
    inattention_weight: float = 1.3
    phone_weight: float = 1.5
    drowsiness_weight: float = 1.7
    drowsy_inattention_weight: float = 2.0
    red_light_high_speed_mps: float = 30 / 3.6
    yellow_light_high_speed_mps: float = 40 / 3.6
    max_context_skew_ms: int = 200
    release_hold_ms: int = 500
    state_gap_reset_ms: int = 1_000
    duplicate_cooldown_ms: int = 900
    event_ttl_ms: int = 1_500

    def __post_init__(self) -> None:
        if (not isinstance(self.road_camera_ids, (tuple, list)) or not self.road_camera_ids
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.road_camera_ids)):
            raise ValueError("road_camera_ids must contain non-empty camera IDs")
        object.__setattr__(self, "road_camera_ids", tuple(self.road_camera_ids))
        for item in fields(self):
            if item.name == "road_camera_ids":
                continue
            value = getattr(self, item.name)
            self._validate_number(item.name, value)
            if item.name.endswith("_ms") and not isinstance(value, int):
                raise ValueError(f"{item.name} must be an integer")
        for name in ("distraction_threshold", "drowsiness_threshold"):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be in [0, 1]")
        for names in (
            ("caution_score", "warning_score", "emergency_score"),
            ("caution_exit_score", "warning_exit_score", "emergency_exit_score"),
        ):
            values = [getattr(self, name) for name in names]
            if values[0] <= 0 or any(left >= right for left, right in zip(values, values[1:])):
                raise ValueError(f"thresholds must be positive and increasing: {names}")
        for level in ("caution", "warning", "emergency"):
            if getattr(self, f"{level}_exit_score") >= getattr(self, f"{level}_score"):
                raise ValueError(f"{level} exit score must be below its entry score")
        if self.event_ttl_ms == 0 or self.state_gap_reset_ms == 0:
            raise ValueError("event TTL and state gap must be positive")

    @staticmethod
    def _validate_number(name: str, value: object) -> None:
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0):
            raise ValueError(f"{name} must be a finite non-negative number")


def load_risk_policy(path: str | Path) -> RiskPolicy:
    """Load rule settings without device profiles or any communication library."""
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if (not isinstance(config, dict) or type(config.get("schema_version")) is not int
            or config.get("schema_version") != 1):
        raise ValueError("risk config requires schema_version: 1")
    if set(config) != {"schema_version", "risk"} or not isinstance(config["risk"], dict):
        raise ValueError("risk config requires only schema_version and a risk mapping")
    unknown = set(config["risk"]) - {item.name for item in fields(RiskPolicy)}
    if unknown:
        raise ValueError(f"unknown risk settings: {sorted(unknown)}")
    return RiskPolicy(**config["risk"])
