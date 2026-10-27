"""Strict encoding of the shared 36-field final sensor contract."""

from __future__ import annotations

import math
from numbers import Real

import numpy as np

from ..sensor_features import (
    CATEGORIES,
    CONTRACT_VERSION,
    GROUPS,
    RAW_FIELDS,
    SENSOR_FIELDS,
    VALIDITY_FIELDS,
    missing_fields,
)
from .common import CLASSES

BOOL_FIELDS = (
    "stop_sign_detected",
    "central_object_present",
    "looking_forward",
    "phone_detected",
    "brake_pressed",
    "is_night",
    *VALIDITY_FIELDS,
)
NUM_FIELDS = tuple(x for x in RAW_FIELDS if x not in CATEGORIES and x not in BOOL_FIELDS)
FEATURE_NAMES = (
    *(f"{name}_{value}" for name, values in CATEGORIES.items() for value in values),
    *NUM_FIELDS,
    *BOOL_FIELDS,
)
BOUNDS = {name: (0, None) for name in NUM_FIELDS}
BOUNDS.update(
    max_bbox_area=(0, 1),
    attention_score=(0, 1),
    drowsiness_score=(0, 1),
    speed_mps=(0, 120),
    accelerator_pct=(0, 100),
    speed_accel_mps2=(None, None),
    accelerator_rate_pct_s=(None, None),
)


def normalize(sample):
    if not isinstance(sample, dict):
        raise TypeError("Sensor input must be a JSON object")
    if set(sample) != set(RAW_FIELDS):
        raise ValueError("Feature contract v3 requires exactly the 36 declared input fields")
    row = {}
    for name in RAW_FIELDS:
        value = sample[name]
        if value is None:
            if name in VALIDITY_FIELDS:
                raise ValueError(f"{name} must be an explicit validity boolean")
            row[name] = None
        elif name in CATEGORIES:
            if value not in CATEGORIES[name]:
                raise ValueError(f"Invalid {name}: {value}")
            row[name] = value
        elif name in BOOL_FIELDS:
            if not isinstance(value, (bool, np.bool_)):
                raise ValueError(f"{name} must be boolean or null")
            row[name] = bool(value)
        else:
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                raise ValueError(f"{name} must be a finite number or null")
            value = float(value)
            lower, upper = BOUNDS[name]
            if (
                not math.isfinite(value)
                or (lower is not None and value < lower)
                or (upper is not None and value > upper)
            ):
                raise ValueError(f"{name} is outside its valid range")
            if name.endswith("_count") and not value.is_integer():
                raise ValueError(f"{name} must be an integer")
            row[name] = int(value) if name.endswith("_count") else value
    for valid, names in GROUPS.items():
        if not row[valid] and any(row[name] is not None for name in names):
            raise ValueError(f"Invalid {valid} group must remain null")
        optional = {
            "traffic_light",
            "audio_direction",
            "weather",
            "visibility_m",
            "speed_accel_mps2",
            "acceleration_duration_ms",
            "accelerator_rate_pct_s",
            "pedal_rate_duration_ms",
        }
        if row[valid] and any(row[name] is None for name in names if name not in optional):
            raise ValueError(f"Valid {valid} group is missing required observations")
    if row["audio_kind"] not in {"siren", "horn"} and row["audio_direction"] is not None:
        raise ValueError("Direction requires a siren/horn event")
    for rate, duration in (
        ("speed_accel_mps2", "acceleration_duration_ms"),
        ("accelerator_rate_pct_s", "pedal_rate_duration_ms"),
    ):
        if (row[rate] is None) != (row[duration] is None):
            raise ValueError(f"{rate} and {duration} must have the same observation validity")
    return row, list(missing_fields(row))


def schema():
    return {
        "schema_version": CONTRACT_VERSION,
        "raw_fields": list(RAW_FIELDS),
        "feature_names": list(FEATURE_NAMES),
        "classes": CLASSES,
        "hybrid": False,
        "category_encoding": {k: list(v) for k, v in CATEGORIES.items()},
        "validity_groups": {k: list(v) for k, v in GROUPS.items()},
        "missing": "JSON null -> NaN; false validity never substitutes normal observations",
        "units": {
            "speed_mps": "m/s",
            "accelerator_pct": "%",
            "speed_accel_mps2": "m/s^2",
            "accelerator_rate_pct_s": "percentage points/s",
            "behavior_duration_ms": "ms",
            "acceleration_duration_ms": "ms",
            "pedal_rate_duration_ms": "ms",
            "precipitation_mm_h": "mm/h",
            "snowfall_cm_h": "cm/h",
            "visibility_m": "m",
            "max_bbox_area": "normalized image area [0,1]",
        },
        "temporal_features": "RiskEngine.last_features; actual per-signal timestamps; max 32 samples",
        "label_mapping": {
            "no_candidate": "LOW",
            "INFO": "MEDIUM",
            "WARNING": "HIGH",
            "CRITICAL": "HIGH",
        },
    }


def validate_schema(value):
    expected = schema()
    if any(value.get(key) != expected[key] for key in expected):
        raise ValueError("Incompatible final sensor contract; regenerate with feature version 3")


def vector(sample, metadata):
    row, _ = normalize(sample)
    values = {
        name: np.nan if row[name] is None else float(row[name])
        for name in (*NUM_FIELDS, *BOOL_FIELDS)
    }
    for name, categories in CATEGORIES.items():
        values.update(
            {
                f"{name}_{value}": np.nan if row[name] is None else float(row[name] == value)
                for value in categories
            }
        )
    return np.asarray([values[name] for name in metadata["feature_names"]], dtype=np.float32)


def usable(row):
    return any(row[name] is not None for name in SENSOR_FIELDS)


def probabilities(model, values):
    """Enforce equal risk for left/right while retaining direction for the HMI.

    Finite synthetic sessions can accidentally correlate a side with risk. Average
    both directional predictions; all other features and missingness stay fixed.
    """
    result = model.inplace_predict(values)
    left = FEATURE_NAMES.index("audio_direction_left")
    right = FEATURE_NAMES.index("audio_direction_right")
    known = np.isfinite(values[:, left]) & np.isfinite(values[:, right])
    if known.any():
        mirrored = values[known].copy()
        mirrored[:, [left, right]] = mirrored[:, [right, left]]
        result[known] = (result[known] + model.inplace_predict(mirrored)) / 2
    return result
