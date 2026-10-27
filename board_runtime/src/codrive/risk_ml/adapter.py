"""Project the integrated DDS schema v1 to ML feature contract v2.

CAN and front perception remain excluded until their producers are validated.
Audio is deliberately withheld from ML while the bench false-positive audit is pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..fusion import FusionPolicy
from ..models import SituationSnapshot

RAW_FIELDS = (
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
)


@dataclass(frozen=True, slots=True)
class AdapterPolicy:
    freshness: FusionPolicy = field(default_factory=FusionPolicy)
    # An explicit per-signal proof is required; can_valid alone cannot establish it.
    verified_vehicle_fields: tuple[str, ...] = ()
    drowsiness_threshold: float = 0.7

    def __post_init__(self) -> None:
        if not set(self.verified_vehicle_fields) <= {
            "vehicle_speed",
            "accelerator_pct",
            "brake_pressed",
        }:
            raise ValueError("Unrecognized vehicle feature")
        if not 0 <= self.drowsiness_threshold <= 1:
            raise ValueError("drowsiness_threshold must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    inputs: dict[str, Any]
    missing_fields: tuple[str, ...]
    withheld_sources: tuple[str, ...]
    valid_until_ns: int


def adapt_snapshot(
    snapshot: SituationSnapshot,
    *,
    now_ns: int,
    policy: AdapterPolicy | None = None,
) -> FeatureFrame:
    policy = policy or AdapterPolicy()
    row: dict[str, Any] = dict.fromkeys(RAW_FIELDS)
    deadlines: list[int] = []

    def fresh(sample, ttl_ms):
        return (
            sample is not None
            and sample.header.capture_time_ns
            <= now_ns
            < sample.header.capture_time_ns + ttl_ms * 1_000_000
        )

    driver = snapshot.driver
    if fresh(driver, policy.freshness.driver_ttl_ms) and driver.face_detected:
        # eyes_closed/microsleep are unsupported by the current model; do not use them.
        row.update(
            driver_drowsy=(driver.behavior in {"drowsy", "head_drop"} if driver.behavior is not None
                           else driver.drowsiness_score >= policy.drowsiness_threshold),
            phone_usage=driver.phone_detected,
            eyes_on_road=driver.looking_forward,
            distraction_score=1.0 - driver.attention_score,
        )
        deadlines.append(driver.header.capture_time_ns + policy.freshness.driver_ttl_ms * 1_000_000)

    vehicle = snapshot.vehicle
    if (
        fresh(vehicle, policy.freshness.vehicle_ttl_ms)
        and vehicle.can_valid
        and policy.verified_vehicle_fields
    ):
        values = {
            "vehicle_speed": vehicle.speed_mps * 3.6,
            "accelerator_pct": vehicle.accelerator_pct,
            "brake_pressed": vehicle.brake_pressed,
        }
        row.update({name: values[name] for name in policy.verified_vehicle_fields})
        deadlines.append(
            vehicle.header.capture_time_ns + policy.freshness.vehicle_ttl_ms * 1_000_000
        )

    environment = snapshot.environment
    if (fresh(environment, policy.freshness.environment_ttl_ms) and environment.api_valid
            and (environment.weather_valid_until_ns is None or
                 (environment.weather_time_ns is not None and
                  environment.weather_time_ns <= now_ns < environment.weather_valid_until_ns))):
        if environment.weather.value != "unknown":
            row["weather"] = environment.weather.value.upper()
        if environment.daylight_valid is not False:
            row["is_night"] = environment.is_night
        deadlines.append(
            environment.header.capture_time_ns + policy.freshness.environment_ttl_ms * 1_000_000
        )

    # Do not map no front camera to zero counts/NONE, or withheld audio to false.
    withheld = ["external_camera", "audio"]
    if not policy.verified_vehicle_fields:
        withheld.append("vehicle")
    return FeatureFrame(
        row,
        tuple(name for name, value in row.items() if value is None),
        tuple(withheld),
        min(deadlines) if deadlines else now_ns,
    )
