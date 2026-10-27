"""Version 2: fifteen observable inputs; km/h, pedal percent, no ranging."""

from __future__ import annotations

import math
from numbers import Real

import numpy as np

from .common import CLASSES

CONTRACT_VERSION = 2
LIGHTS = ["RED", "YELLOW", "GREEN", "NONE"]
DIRECTIONS = ["LEFT", "RIGHT"]
WEATHERS = ["CLEAR", "CLOUDY", "RAIN", "SNOW", "FOG", "THUNDER"]
CATEGORY_FIELDS = {"traffic_light": LIGHTS, "audio_direction": DIRECTIONS, "weather": WEATHERS}
NUM_FIELDS = [
    "pedestrian_count",
    "vehicle_count",
    "vehicle_speed",
    "accelerator_pct",
    "distraction_score",
]
BOOL_FIELDS = [
    "brake_pressed",
    "driver_drowsy",
    "phone_usage",
    "eyes_on_road",
    "siren_detected",
    "horn_detected",
    "is_night",
]
RAW_FIELDS = [
    "traffic_light",
    "pedestrian_count",
    "vehicle_count",
    "vehicle_speed",
    "accelerator_pct",
    "brake_pressed",
    "driver_drowsy",
    "phone_usage",
    "eyes_on_road",
    "distraction_score",
    "siren_detected",
    "horn_detected",
    "audio_direction",
    "weather",
    "is_night",
]
BASE_FEATURES = [
    *(f"{name}_{value}" for name, categories in CATEGORY_FIELDS.items() for value in categories),
    *NUM_FIELDS,
    *BOOL_FIELDS,
]
HYBRID_FEATURES = [*BASE_FEATURES, "rule_risk_score", "rule_risk_level"]
REMOVED_FIELDS = {
    "nearest_vehicle_distance",
    "nearest_pedestrian_distance",
    "relative_speed",
    "ttc",
    "distance_to_stop_line",
    "lane_departure",
    "pedestrian_in_path",
}
BOUNDS = {name: (0, None) for name in NUM_FIELDS}
BOUNDS.update(vehicle_speed=(0, 432), accelerator_pct=(0, 100), distraction_score=(0, 1))


def nullable(value):
    return value is None or (isinstance(value, Real) and math.isnan(float(value)))


def normalize(sample):
    if not isinstance(sample, dict):
        raise TypeError("Sensor input must be a JSON object")
    obsolete = REMOVED_FIELDS.intersection(sample)
    if obsolete:
        raise ValueError(
            f"Legacy distance/path inputs are unsupported; use the 15-input contract: {sorted(obsolete)}"
        )
    result, missing = {}, []
    for name in RAW_FIELDS:
        value = sample.get(name)
        if name in CATEGORY_FIELDS and isinstance(value, str) and value.upper() == "UNKNOWN":
            value = None
        if nullable(value):
            result[name] = None
            missing.append(name)
            continue
        if name in CATEGORY_FIELDS:
            if not isinstance(value, str) or value.upper() not in CATEGORY_FIELDS[name]:
                raise ValueError(f"{name} must be one of {CATEGORY_FIELDS[name]} or null")
            result[name] = value.upper()
        elif name in BOOL_FIELDS:
            if not isinstance(value, (bool, np.bool_)):
                raise ValueError(f"{name} must be boolean or null")
            result[name] = bool(value)
        else:
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                raise ValueError(f"{name} must be a finite number or null")
            value = float(value)
            lower, upper = BOUNDS[name]
            if not math.isfinite(value) or value < lower or (upper is not None and value > upper):
                raise ValueError(f"{name} is outside its valid range")
            if name.endswith("_count") and not value.is_integer():
                raise ValueError(f"{name} must be an integer")
            result[name] = int(value) if name.endswith("_count") else value
    if result["siren_detected"] is False and result["horn_detected"] is False:
        if result["audio_direction"] is not None:
            raise ValueError("audio_direction must be null when no audio event is detected")
        missing.remove("audio_direction")
    return result, missing


def schema(hybrid=False):
    return {
        "schema_version": CONTRACT_VERSION,
        "feature_names": HYBRID_FEATURES if hybrid else BASE_FEATURES,
        "hybrid": hybrid,
        "classes": CLASSES,
        "raw_fields": RAW_FIELDS,
        "category_encoding": {name: categories for name, categories in CATEGORY_FIELDS.items()},
        "missing": "JSON null/UNKNOWN -> NaN; no false/zero substitution",
        "units": {"vehicle_speed": "km/h", "accelerator_pct": "%"},
        "audio_direction": "LEFT/RIGHT; absent/unknown is null; shared direction for a single audio event",
    }


def validate_schema(metadata):
    expected = schema(hybrid=metadata.get("hybrid", False))
    for key in ("schema_version", "raw_fields", "feature_names", "classes", "category_encoding"):
        if metadata.get(key) != expected[key]:
            raise ValueError(
                "Incompatible model/dataset contract; regenerate and retrain with schema version 2"
            )


def vector(sample, metadata, rule=None):
    validate_schema(metadata)
    row, _ = normalize(sample)
    values = {
        name: np.nan if row[name] is None else float(row[name]) for name in NUM_FIELDS + BOOL_FIELDS
    }
    for name, categories in CATEGORY_FIELDS.items():
        values.update(
            {
                f"{name}_{category}": np.nan if row[name] is None else float(row[name] == category)
                for category in categories
            }
        )
    if metadata["hybrid"]:
        if rule is None:
            raise ValueError("Hybrid inference requires computed RuleEngine results")
        values.update(rule_risk_score=rule["score"], rule_risk_level=CLASSES.index(rule["level"]))
    return np.asarray([values[name] for name in metadata["feature_names"]], dtype=np.float32)


def matrix(frame, metadata):
    rows = []
    for row in frame.to_dict("records"):
        rule = (
            {"score": row["rule_risk_score"], "level": row["rule_risk_level"]}
            if metadata["hybrid"]
            else None
        )
        rows.append(vector(row, metadata, rule))
    return np.stack(rows)
