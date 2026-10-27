"""Final-input parity, temporal continuity, missing sensors and trained artifacts."""

from __future__ import annotations

import gzip
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codrive.models import (
    AudioEvent,
    AudioKind,
    BoundingBox,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    SituationSnapshot,
    TrackedObject,
    TrafficLight,
    VehicleState,
    Weather,
    from_json,
)
from codrive.v3_feature_engine import RiskEngine, RiskPolicy
from codrive.risk_ml.adapter_v3 import FinalSensorAdapter
from codrive.risk_ml.shadow import ShadowRisk
from codrive.sensor_features import RAW_FIELDS, SENSOR_FIELDS, VALIDITY_FIELDS

NOW = 1_700_000_000_000_000_000
MODEL = Path(__file__).resolve().parents[2] / "artifacts/models/risk/final-sensors-v3"


def h(now=NOW, source="test", sequence=1, boot="boot"):
    return Header(source, sequence, now, now, now, boot_id=boot)


def vehicle(now=NOW, speed=10, pedal=20, *, signals=None, boot="boot"):
    return VehicleState(
        h(now, boot=boot),
        speed,
        0,
        False,
        pedal,
        signal_times_ns=(
            tuple((name, now) for name in ("speed_mps", "accelerator_pct", "brake_pressed"))
            if signals is None
            else signals
        ),
    )


def sample(now=NOW, **kwargs):
    return SituationSnapshot(h(now), **kwargs)


def driver(now=NOW, behavior="phone"):
    return DriverState(h(now), "cabin", "test", 0.2, False, phone_detected=True, behavior=behavior)


def test_shared_final_observations_include_all_sensor_groups():
    pytest.importorskip("numpy")
    from codrive.risk_ml.features_v3 import FEATURE_NAMES, normalize, schema, vector

    engine = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
    front = ExternalPerception(
        h(),
        "front",
        "test",
        traffic_light=TrafficLight.RED,
        objects=tuple(
            TrackedObject(i, name, 0.8, BoundingBox(0.4, 0.2, 0.6, 0.8))
            for i, name in enumerate(
                ("person", "car", "bus", "truck", "bicycle", "motorcycle", "stop_sign")
            )
        ),
    )
    env = EnvironmentState(
        h(),
        weather=Weather.FOG,
        is_night=True,
        api_valid=True,
        daylight_valid=True,
        precipitation_mm_h=1,
        snowfall_cm_h=0.2,
        visibility_m=80,
    )
    snap = sample(
        vehicle=vehicle(),
        external=(front,),
        driver=driver(),
        environment=env,
        audio=AudioEvent(h(), AudioKind.HORN, 0.9, 960, "test", "right"),
    )
    engine.evaluate(snap, now_ns=NOW)
    frame = FinalSensorAdapter().adapt(snap, now_ns=NOW, features=engine.last_features)
    row, _ = normalize(frame.inputs)
    assert len(row) == 36 and len(SENSOR_FIELDS) == 28 and len(VALIDITY_FIELDS) == 8
    assert all(row[name] for name in VALIDITY_FIELDS)
    assert [row[f"{k}_count"] for k in ("pedestrian", "vehicle", "bicycle", "motorcycle")] == [
        1,
        3,
        1,
        1,
    ]
    assert row["stop_sign_detected"] and row["central_object_present"]
    assert row["max_bbox_area"] == pytest.approx(0.12)
    assert row["audio_direction"] == "right" and row["visibility_m"] == 80
    assert row["speed_mps"] == 10  # v3 is m/s, unlike legacy v2 km/h.
    assert row["speed_accel_mps2"] is None  # A single sample cannot establish acceleration.
    assert vector(row, schema()).shape == (len(FEATURE_NAMES),)
    assert "rule_risk_score" not in FEATURE_NAMES
    assert frame.valid_until_ns == NOW + 120_000_000


def test_duplicate_can_cannot_advance_rate_duration_and_gap_resets_history():
    engine = RiskEngine()
    for i in range(7):
        now = NOW + i * 100_000_000
        engine.evaluate(sample(now, vehicle=vehicle(now, 10 + i * 0.4)), now_ns=now)
    row = engine.last_features
    assert row["speed_accel_mps2"] == pytest.approx(4)
    assert row["acceleration_duration_ms"] == 500
    assert any(c.code == "rapid_acceleration" for c in engine.last_candidates)
    last = vehicle(now, 10 + i * 0.4)
    engine.evaluate(sample(now + 50_000_000, vehicle=last), now_ns=now + 50_000_000)
    assert engine.last_features["acceleration_duration_ms"] == 500
    now += 300_000_000
    engine.evaluate(sample(now, vehicle=vehicle(now, 15)), now_ns=now)
    assert engine.last_features["speed_accel_mps2"] is None
    assert engine.last_features["acceleration_duration_ms"] is None
    engine.evaluate(sample(now, vehicle=vehicle(now, 15, boot="new-boot")), now_ns=now)
    assert engine.last_features["speed_accel_mps2"] is None


def test_signal_defaults_and_face_loss_are_not_normal_features():
    adapter = FinalSensorAdapter()
    snap = sample(
        vehicle=vehicle(signals=(("speed_mps", NOW),)),
        driver=replace(driver(), face_detected=False),
    )
    row = adapter.adapt(snap, now_ns=NOW).inputs
    assert row["speed_valid"]
    assert not row["pedal_valid"] and not row["brake_valid"] and not row["driver_valid"]
    assert row["accelerator_pct"] is row["brake_pressed"] is row["driver_behavior"] is None
    legacy = replace(snap, vehicle=replace(snap.vehicle, signal_times_ns=()))
    assert FinalSensorAdapter().adapt(legacy, now_ns=NOW).inputs["speed_mps"] is None


def test_weather_expiry_keeps_independent_night_and_deadline():
    env = EnvironmentState(
        h(),
        weather=Weather.RAIN,
        api_valid=True,
        is_night=True,
        daylight_valid=True,
        weather_time_ns=NOW - 1_000_000_000,
        weather_valid_until_ns=NOW + 1_000_000,
    )
    adapter = FinalSensorAdapter()
    frame = adapter.adapt(sample(environment=env), now_ns=NOW)
    assert frame.valid_until_ns == NOW + 1_000_000
    expired = adapter.adapt(sample(environment=env), now_ns=NOW + 2_000_000).inputs
    assert expired["weather"] is None and not expired["weather_valid"]
    assert expired["is_night"] is True and expired["daylight_valid"]
    stale = adapter.adapt(sample(environment=env), now_ns=NOW + 3_001_000_000).inputs
    assert not stale["daylight_valid"] and stale["is_night"] is None


def test_missing_audio_is_distinct_from_observed_silence_and_unknown_direction():
    adapter = FinalSensorAdapter()
    assert adapter.adapt(sample(), now_ns=NOW).inputs["audio_kind"] is None
    silence = AudioEvent(h(), AudioKind.NONE, 0.9, 960, "test")
    frame = adapter.adapt(sample(audio=silence), now_ns=NOW)
    assert frame.inputs["audio_kind"] == "none" and frame.inputs["audio_valid"]
    assert "audio_direction" not in frame.missing_fields
    unknown = replace(silence, kind=AudioKind.SIREN)
    frame = adapter.adapt(sample(audio=unknown), now_ns=NOW)
    assert frame.inputs["audio_direction"] is None
    assert "audio_direction" in frame.missing_fields


def test_v3_rejects_old_units_invalid_groups_and_nonfinite_values():
    pytest.importorskip("numpy")
    from codrive.risk_ml.features_v3 import normalize

    row = FinalSensorAdapter().adapt(sample(), now_ns=NOW).inputs
    for invalid in (
        {**row, "speed_mps": 10},
        {**row, "speed_valid": None},
        {**row, "vehicle_speed": 36},
        {**row, "speed_valid": True, "speed_mps": float("nan")},
    ):
        with pytest.raises(ValueError):
            normalize(invalid)


def test_v3_worker_does_not_treat_false_validity_flags_as_sensor_observations():
    class Stub:
        contract_version = 3

        def predict(self, row):
            raise AssertionError("All missing observations must not be sent to the model")

    worker = ShadowRisk(Stub())
    try:
        result = worker.update(sample(), now_ns=NOW)
        assert result["status"] == "unavailable"
        assert result["xgboost_risk_level"] is None
        assert result["feature_contract_version"] == 3
    finally:
        worker.close()


def test_v3_worker_coalesces_at_ten_hz_without_blocking_updates():
    calls, entered = [], threading.Event()

    class Stub:
        contract_version = 3

        def predict(self, row):
            calls.append(time.monotonic())
            entered.set()
            return {"status": "ok", "xgboost_risk_level": "HIGH"}

    worker = ShadowRisk(Stub())
    try:
        worker.update(sample(driver=driver()), now_ns=NOW)
        assert entered.wait(1)
        for i in range(1, 5):
            now = NOW + i * 20_000_000
            worker.update(sample(now, driver=driver(now)), now_ns=now)
        until = time.monotonic() + 1
        while len(calls) < 2 and time.monotonic() < until:
            time.sleep(0.005)
        assert len(calls) == 2
        assert calls[1] - calls[0] >= 0.095
    finally:
        worker.close()


def test_v3_changing_stream_shows_only_valid_prediction_with_its_original_inputs():
    class Stub:
        contract_version = 3

        def predict(self, row):
            return {
                "status": "ok",
                "xgboost_risk_level": "HIGH",
                "duration_used": row["behavior_duration_ms"],
            }

    worker = ShadowRisk(Stub())
    seen = []
    try:
        for i in range(14):
            now = NOW + i * 20_000_000
            seen.append(
                worker.update(sample(now, driver=driver(now), vehicle=vehicle(now)), now_ns=now)
            )
            time.sleep(0.02)
        valid = [r for r in seen if r["status"] == "ok"]
        assert len(valid) >= 8
        assert any(r["latest_input_pending"] for r in valid)
        for result in valid:
            assert result["duration_used"] == result["inputs"]["behavior_duration_ms"]
            assert result["prediction_age_ms"] <= 120
        absent = worker.update(sample(now), now_ns=now)
        assert absent["status"] == "unavailable" and absent["xgboost_risk_level"] is None
    finally:
        worker.close()


@pytest.fixture
def trained():
    pytest.importorskip("xgboost")
    if not (MODEL / "model.ubj").exists():
        pytest.skip("Run python -m codrive.risk_ml.train to generate final sensor artifacts")
    from codrive.risk_ml.infer import Predictor

    return Predictor(MODEL)


def test_trained_model_contract_and_missing_inputs(trained):
    assert trained.contract_version == 3
    assert trained.preprocessing["raw_fields"] == list(RAW_FIELDS)
    empty = FinalSensorAdapter().adapt(sample(), now_ns=NOW).inputs
    with pytest.raises(ValueError, match="No usable"):
        trained.predict(empty)
    audio = AudioEvent(h(), AudioKind.SIREN, 0.95, 960, "test", "left")
    row = FinalSensorAdapter().adapt(sample(audio=audio), now_ns=NOW).inputs
    result = trained.predict(row)
    assert result["xgboost_risk_level"] == "HIGH"
    assert sum(result["risk_probability"].values()) == pytest.approx(1)
    assert "왼쪽" in result["warning_message"] and result["degraded"]
    mirrored = trained.predict({**row, "audio_direction": "right"})
    assert mirrored["risk_probability"] == result["risk_probability"]
    assert "오른쪽" in mirrored["warning_message"]


def test_saved_dataset_replays_exact_features_labels_and_session_splits(trained):
    metadata = trained.metadata
    policy = RiskPolicy(**metadata["rule_policy"])
    groups = {name: set() for name in ("train", "validation", "test")}
    previous_session, engine, count = None, None, 0
    with (
        (MODEL / "data/dataset.jsonl").open() as records,
        gzip.open(MODEL / "data/sensor_stream.jsonl.gz", "rt") as stream,
    ):
        for record, raw in zip(records, stream, strict=True):
            item = json.loads(record)
            if item["session_id"] != previous_session:
                engine = RiskEngine(policy=policy)
                previous_session = item["session_id"]
            snapshot = from_json(SituationSnapshot, raw)
            engine.evaluate(snapshot, now_ns=item["time_ns"])
            assert engine.last_features == item["inputs"]
            severity = max((int(c.severity) for c in engine.last_candidates), default=0)
            expected = "LOW" if severity == 0 else "MEDIUM" if severity == 1 else "HIGH"
            assert expected == item["target"]
            groups[item["split"]].add(item["session_id"])
            count += 1
    assert count == metadata["training_samples"] == 10000
    assert [len(groups[name]) for name in ("train", "validation", "test")] == [140, 30, 30]
    assert not (
        groups["train"] & groups["test"]
        or groups["train"] & groups["validation"]
        or groups["validation"] & groups["test"]
    )


def test_saved_test_probabilities_match_loaded_cpu_predictor(trained):
    from codrive.risk_ml.common import sha256

    manifest = json.loads((MODEL / "manifest.json").read_text())
    for name, digest in manifest.items():
        assert sha256(MODEL / name) == digest
    dataset = [json.loads(line) for line in (MODEL / "data/dataset.jsonl").read_text().splitlines()]
    rows = [item for item in dataset if item["split"] == "test"]
    predictions = [
        json.loads(line)
        for line in (MODEL / "data/test_predictions.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(predictions) == 1500
    for item, recorded in zip(rows, predictions, strict=True):
        assert (item["session_id"], item["frame"]) == (recorded["session_id"], recorded["frame"])
        result = trained.predict(item["inputs"])
        assert result["xgboost_risk_level"] == recorded["prediction"]
        assert result["risk_probability"] == pytest.approx(recorded["probabilities"], abs=1e-7)
