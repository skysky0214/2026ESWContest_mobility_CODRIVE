"""Validated tuning parameters for the local rule engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    road_camera_ids: tuple[str, ...] = ("front",)
    object_base_scores: Mapping[str, float] = field(default_factory=lambda: {
        "pedestrian": 40.0, "bicycle": 35.0, "motorcycle": 30.0,
        "vehicle": 25.0, "obstacle": 20.0,
    })
    direction_weights: Mapping[str, float] = field(default_factory=lambda: {
        "front": 1.5, "front_left": 1.2, "front_right": 1.2,
        "left": 0.8, "right": 0.8, "blind_spot": 0.8,
        "rear": 0.5, "rear_left": 0.5, "rear_right": 0.5,
    })
    object_min_confidence: float = 0.5
    signal_min_confidence: float = 0.65
    lane_min_confidence: float = 0.5
    audio_min_confidence: float = 0.7
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
    near_distance_m: float = 5.0
    middle_distance_m: float = 10.0
    far_distance_m: float = 20.0
    near_distance_weight: float = 2.0
    middle_distance_weight: float = 1.5
    far_distance_weight: float = 1.0
    distant_weight: float = 0.5
    caution_score: float = 30.0
    warning_score: float = 60.0
    emergency_score: float = 90.0
    caution_exit_score: float = 20.0
    warning_exit_score: float = 45.0
    emergency_exit_score: float = 75.0
    collision_emergency_ttc_s: float = 1.5
    collision_warning_ttc_s: float = 3.0
    collision_caution_ttc_s: float = 5.0
    closing_speed_epsilon_mps: float = 0.1
    minimum_motion_mps: float = 1.4
    high_speed_mps: float = 40 / 3.6
    high_speed_weight: float = 1.2
    braking_weight: float = 0.8
    distraction_threshold: float = 0.35
    drowsiness_threshold: float = 0.7
    eyes_closed_hold_ms: int = 800
    inattention_weight: float = 1.3
    phone_weight: float = 1.5
    drowsiness_weight: float = 1.7
    drowsy_inattention_weight: float = 2.0
    lane_warning_offset_m: float = 0.55
    lane_critical_offset_m: float = 0.925
    red_light_high_speed_mps: float = 30 / 3.6
    yellow_light_high_speed_mps: float = 40 / 3.6
    hard_acceleration_mps2: float = 4.0
    accelerator_mismatch_pct: float = 90.0
    acceleration_confirm_ms: int = 500
    lane_confirm_ms: int = 250
    max_context_skew_ms: int = 200
    release_hold_ms: int = 500
    state_gap_reset_ms: int = 1_000
    duplicate_cooldown_ms: int = 900
    event_ttl_ms: int = 1_500

    environment_ttl_ms: int = 3000
    night_weight: float = 1.15
    rain_weight: float = 1.2
    snow_weight: float = 1.35
    fog_weight: float = 1.4
    thunder_weight: float = 1.35
    visibility_warning_m: float = 1000.0
    visibility_critical_m: float = 200.0
    visibility_max_weight: float = 1.6
    environment_weight_cap: float = 2.0

    def __post_init__(self) -> None:
        if (not isinstance(self.road_camera_ids, (tuple, list)) or not self.road_camera_ids
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.road_camera_ids)):
            raise ValueError("road_camera_ids must contain non-empty camera IDs")
        object.__setattr__(self, "road_camera_ids", tuple(self.road_camera_ids))
        for name in ("object_base_scores", "direction_weights"):
            mapping = getattr(self, name)
            if not isinstance(mapping, Mapping) or not mapping:
                raise ValueError(f"{name} must be a non-empty mapping")
            for key, value in mapping.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{name} keys must be non-empty strings")
                self._validate_number(f"{name}.{key}", value)
            object.__setattr__(self, name, MappingProxyType(dict(mapping)))
        for item in fields(self):
            if item.name in {"object_base_scores", "direction_weights", "road_camera_ids"}:
                continue
            value = getattr(self, item.name)
            self._validate_number(item.name, value)
            if item.name.endswith("_ms") and not isinstance(value, int):
                raise ValueError(f"{item.name} must be an integer")
        for name in ("object_min_confidence", "signal_min_confidence", "lane_min_confidence",
                     "audio_min_confidence", "distraction_threshold", "drowsiness_threshold"):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be in [0, 1]")
        for names in (
            ("caution_score", "warning_score", "emergency_score"),
            ("caution_exit_score", "warning_exit_score", "emergency_exit_score"),
            ("collision_emergency_ttc_s", "collision_warning_ttc_s", "collision_caution_ttc_s"),
            ("near_distance_m", "middle_distance_m", "far_distance_m"),
            ("lane_warning_offset_m", "lane_critical_offset_m"),
        ):
            values = [getattr(self, name) for name in names]
            if values[0] <= 0 or any(left >= right for left, right in zip(values, values[1:])):
                raise ValueError(f"thresholds must be positive and increasing: {names}")
        for name in ("night_weight", "rain_weight", "snow_weight", "fog_weight",
                     "thunder_weight", "visibility_max_weight", "environment_weight_cap"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.visibility_warning_m <= self.visibility_critical_m:
            raise ValueError("visibility warning must exceed critical distance")
        if self.environment_ttl_ms <= 0:
            raise ValueError("environment TTL must be positive")
        for level in ("caution", "warning", "emergency"):
            if getattr(self, f"{level}_exit_score") >= getattr(self, f"{level}_score"):
                raise ValueError(f"{level} exit score must be below its entry score")
        if (self.event_ttl_ms == 0 or self.closing_speed_epsilon_mps == 0
                or self.state_gap_reset_ms == 0):
            raise ValueError("event TTL, state gap, and closing-speed epsilon must be positive")

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
