"""Final sensor feature contract, shared by rules, logging and optional ML.

Only the standard library is imported here so rules do not require numpy/XGBoost.
Temporal features are supplied by the rule engine's actual observation history.
"""

from __future__ import annotations

CONTRACT_VERSION = 3
CATEGORIES = {
    "traffic_light": ("RED", "YELLOW", "GREEN", "OFF"),
    "driver_behavior": ("awake", "phone", "drowsy", "head_drop", "distracted"),
    "audio_kind": ("none", "siren", "horn", "other"),
    "audio_direction": ("left", "right"),
    "weather": ("CLEAR", "CLOUDY", "RAIN", "SNOW", "FOG", "THUNDER"),
}
SENSOR_FIELDS = (
    "pedestrian_count",
    "vehicle_count",
    "bicycle_count",
    "motorcycle_count",
    "stop_sign_detected",
    "central_object_present",
    "max_bbox_area",
    "traffic_light",
    "driver_behavior",
    "attention_score",
    "drowsiness_score",
    "looking_forward",
    "phone_detected",
    "behavior_duration_ms",
    "audio_kind",
    "audio_direction",
    "speed_mps",
    "accelerator_pct",
    "brake_pressed",
    "speed_accel_mps2",
    "accelerator_rate_pct_s",
    "acceleration_duration_ms",
    "pedal_rate_duration_ms",
    "weather",
    "is_night",
    "precipitation_mm_h",
    "snowfall_cm_h",
    "visibility_m",
)
VALIDITY_FIELDS = (
    "front_valid",
    "driver_valid",
    "audio_valid",
    "speed_valid",
    "pedal_valid",
    "brake_valid",
    "weather_valid",
    "daylight_valid",
)
RAW_FIELDS = (*SENSOR_FIELDS, *VALIDITY_FIELDS)
GROUPS = {
    "front_valid": SENSOR_FIELDS[:8],
    "driver_valid": SENSOR_FIELDS[8:14],
    "audio_valid": SENSOR_FIELDS[14:16],
    "speed_valid": ("speed_mps", "speed_accel_mps2", "acceleration_duration_ms"),
    "pedal_valid": ("accelerator_pct", "accelerator_rate_pct_s", "pedal_rate_duration_ms"),
    "brake_valid": ("brake_pressed",),
    "weather_valid": ("weather", "precipitation_mm_h", "snowfall_cm_h", "visibility_m"),
    "daylight_valid": ("is_night",),
}


def summarize_objects(perception, policy):
    counts = {"pedestrian": 0, "vehicle": 0, "bicycle": 0, "motorcycle": 0}
    stop_sign, largest, central = False, 0.0, False
    for obj in perception.objects:
        kind = obj.class_name.lower()
        stop_sign |= kind in {"stop_sign", "stop sign"}
        category = (
            "pedestrian"
            if kind in {"person", "pedestrian"}
            else "vehicle"
            if kind in {"car", "vehicle", "bus", "truck"}
            else kind
            if kind in {"bicycle", "motorcycle"}
            else None
        )
        if category:
            counts[category] += 1
            largest = max(largest, (obj.bbox.x2 - obj.bbox.x1) * (obj.bbox.y2 - obj.bbox.y1))
            center = (obj.bbox.x1 + obj.bbox.x2) / 2
            central |= policy.object_center_min <= center <= policy.object_center_max
    return counts, stop_sign, largest, central


def environment_validity(env, now):
    if env is None:
        return False, False
    weather = env.api_valid and (
        env.weather_valid_until_ns is None
        or (
            env.weather_time_ns is not None
            and env.weather_time_ns <= now < env.weather_valid_until_ns
        )
    )
    daylight = env.api_valid if env.daylight_valid is None else env.daylight_valid
    return weather, daylight


def build_inputs(snapshot, *, now, policy, behavior, duration, motion):
    """Project an already TTL-filtered snapshot; do not invent unseen CAN values."""
    row = dict.fromkeys(SENSOR_FIELDS)
    row.update(dict.fromkeys(VALIDITY_FIELDS, False))
    front = [p for p in snapshot.external if p.camera_id in policy.road_camera_ids]
    if front:
        row.update(
            front_valid=True,
            stop_sign_detected=False,
            central_object_present=False,
            max_bbox_area=0.0,
            **{f"{k}_count": 0 for k in ("pedestrian", "vehicle", "bicycle", "motorcycle")},
        )
        lights = []
        for perception in front:
            counts, stop, largest, central = summarize_objects(perception, policy)
            for kind, count in counts.items():
                row[f"{kind}_count"] += count
            row["stop_sign_detected"] |= stop
            row["central_object_present"] |= central
            row["max_bbox_area"] = max(row["max_bbox_area"], largest)
            lights.append(perception.traffic_light.value.upper())
        row["traffic_light"] = next(
            (x for x in ("RED", "YELLOW", "GREEN", "OFF") if x in lights), None
        )
    driver = snapshot.driver
    if driver is not None and driver.face_detected and behavior is not None:
        row.update(
            driver_valid=True,
            driver_behavior=behavior,
            behavior_duration_ms=duration,
            attention_score=driver.attention_score,
            drowsiness_score=driver.drowsiness_score,
            looking_forward=driver.looking_forward,
            phone_detected=driver.phone_detected,
        )
    if snapshot.audio is not None:
        audio = snapshot.audio
        direction = audio.direction.lower()
        row.update(
            audio_valid=True,
            audio_kind=audio.kind.value,
            audio_direction=(
                direction
                if audio.kind.value in {"siren", "horn"} and direction in {"left", "right"}
                else None
            ),
        )
    vehicle = snapshot.vehicle
    if vehicle is not None and vehicle.can_valid:
        stamps = dict(vehicle.signal_times_ns)
        for signal, valid in (
            ("speed_mps", "speed_valid"),
            ("accelerator_pct", "pedal_valid"),
            ("brake_pressed", "brake_valid"),
        ):
            timestamp = stamps.get(signal)
            if timestamp is not None and 0 <= now - timestamp <= policy.vehicle_ttl_ms * 1e6:
                row[valid] = True
                row[signal] = getattr(vehicle, signal)
        row.update(motion)
    env = snapshot.environment
    weather, daylight = environment_validity(env, now)
    row.update(weather_valid=weather, daylight_valid=daylight)
    if weather:
        row.update(
            weather=None if env.weather.value == "unknown" else env.weather.value.upper(),
            precipitation_mm_h=env.precipitation_mm_h,
            snowfall_cm_h=env.snowfall_cm_h,
            visibility_m=env.visibility_m,
        )
    if daylight:
        row["is_night"] = env.is_night
    return {name: row[name] for name in RAW_FIELDS}


def missing_fields(row):
    # A direction is inapplicable for silence/other, rather than a missing sensor.
    return tuple(
        name
        for name in SENSOR_FIELDS
        if row[name] is None
        and not (
            name == "audio_direction"
            and row["audio_valid"]
            and row["audio_kind"] in {"none", "other"}
        )
    )
