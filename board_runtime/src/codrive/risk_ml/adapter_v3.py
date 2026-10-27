"""Use the same final observations as the rule engine, including temporal history."""

from __future__ import annotations

from ..v3_feature_engine import RiskEngine, RiskPolicy
from ..sensor_features import missing_fields
from .adapter import FeatureFrame


class FinalSensorAdapter:
    def __init__(self, *, policy: RiskPolicy | None = None):
        self.engine = RiskEngine(
            policy=policy
            or RiskPolicy(
                environment_ttl_ms=3000,
                cabin_observation_enabled=True,
            )
        )

    def adapt(self, snapshot, *, now_ns, features=None):
        if features is None:
            self.engine.evaluate(snapshot, now_ns=now_ns)
            features = self.engine.last_features
        row = dict(features)
        policy = self.engine.policy
        deadlines = []
        for valid, sample, ttl in (
            (row["front_valid"], None, policy.external_ttl_ms),
            (row["driver_valid"], snapshot.driver, policy.driver_ttl_ms),
            (row["audio_valid"], snapshot.audio, policy.audio_ttl_ms),
            (
                row["weather_valid"] or row["daylight_valid"],
                snapshot.environment,
                policy.environment_ttl_ms,
            ),
        ):
            if valid and sample is not None:
                deadlines.append(sample.header.capture_time_ns + ttl * 1_000_000)
        if row["front_valid"]:
            deadlines.extend(
                p.header.capture_time_ns + policy.external_ttl_ms * 1_000_000
                for p in snapshot.external
                if p.camera_id in policy.road_camera_ids
            )
        if snapshot.vehicle is not None:
            stamps = dict(snapshot.vehicle.signal_times_ns)
            for valid, signal in (
                ("speed_valid", "speed_mps"),
                ("pedal_valid", "accelerator_pct"),
                ("brake_valid", "brake_pressed"),
            ):
                if row[valid]:
                    deadlines.append(stamps[signal] + policy.vehicle_ttl_ms * 1_000_000)
        env = snapshot.environment
        if row["weather_valid"] and env.weather_valid_until_ns is not None:
            deadlines.append(env.weather_valid_until_ns)
        return FeatureFrame(row, missing_fields(row), (), min(deadlines, default=now_ns))


LIMITED_INPUT_NOTICE = ' 일부 센서 정보가 누락되어 판단이 제한됩니다.'


def sensor_missing_fields(frame, snapshot, *, now_ns, policy):
    """Separate absent detections from unavailable sensor streams; keep ML nulls."""
    from ..sensor_features import GROUPS
    expected=set()
    front_fresh=any(p.camera_id in policy.road_camera_ids and
                    0<=now_ns-p.header.capture_time_ns<=policy.external_ttl_ms*1_000_000
                    for p in snapshot.external)
    if frame.inputs.get('front_valid') and front_fresh:
        expected.add('traffic_light')
    driver=snapshot.driver
    if driver is not None and not driver.face_detected and (
            0<=now_ns-driver.header.capture_time_ns<=policy.driver_ttl_ms*1_000_000):
        expected.update(GROUPS['driver_valid'])
    return tuple(name for name in frame.missing_fields if name not in expected)
