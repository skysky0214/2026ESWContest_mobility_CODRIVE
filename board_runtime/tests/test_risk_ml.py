"""Live-contract, expiry and isolation regression checks for optional model display."""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codrive.fusion import FusionPolicy, SituationFusion
from codrive.models import (
    AudioEvent,
    AudioKind,
    DriverState,
    EnvironmentState,
    Header,
    SituationSnapshot,
    VehicleState,
    Weather,
)
from codrive.risk_ml.adapter import RAW_FIELDS, AdapterPolicy, adapt_snapshot
from codrive.risk_ml.shadow import ShadowRisk

NOW = 5_000_000_000_000
MODEL = Path(__file__).resolve().parents[2] / "artifacts/models/risk/B"


def header(source="test", sequence=1, now=NOW):
    return Header.now(source, sequence, capture_time_ns=now)


def snapshot(**kwargs):
    return SituationSnapshot(header(), **kwargs)


def driver(**kwargs):
    values = {
        "attention_score": 0.8,
        "looking_forward": True,
        "phone_detected": False,
        "drowsiness_score": 0.0,
    }
    values.update(kwargs)
    return DriverState(header("jetson-cabin.dms"), "cabin", "dms.pt", **values)


def eventually(update, predicate=lambda result: result["status"] != "pending"):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        result = update()
        if predicate(result):
            return result
        time.sleep(0.005)
    raise AssertionError("Worker did not complete")


def test_missing_all_stays_null_instead_of_normal():
    frame = adapt_snapshot(snapshot(), now_ns=NOW)
    assert tuple(frame.inputs) == RAW_FIELDS
    assert all(value is None for value in frame.inputs.values())
    assert len(frame.missing_fields) == 15


def test_vehicle_conversion_requires_per_signal_verification_and_valid_can():
    vehicle = VehicleState(header(), 10.0, 0.0, False, 25.0)
    snap = snapshot(vehicle=vehicle)
    assert adapt_snapshot(snap, now_ns=NOW).inputs["vehicle_speed"] is None
    policy = AdapterPolicy(verified_vehicle_fields=("vehicle_speed", "brake_pressed"))
    row = adapt_snapshot(snap, now_ns=NOW, policy=policy).inputs
    assert row["vehicle_speed"] == 36.0 and row["brake_pressed"] is False
    assert row["accelerator_pct"] is None
    invalid = replace(snap, vehicle=replace(vehicle, can_valid=False))
    assert adapt_snapshot(invalid, now_ns=NOW, policy=policy).inputs["vehicle_speed"] is None
    assert (
        adapt_snapshot(snap, now_ns=NOW + 121_000_000, policy=policy).inputs["vehicle_speed"]
        is None
    )


@pytest.mark.parametrize("age_ms", [400, 401, -1])
def test_expired_or_future_driver_never_becomes_negative_observation(age_ms):
    row = adapt_snapshot(
        snapshot(driver=driver(phone_detected=True)), now_ns=NOW + age_ms * 1_000_000
    ).inputs
    assert all(
        row[name] is None
        for name in (
            "driver_drowsy",
            "phone_usage",
            "eyes_on_road",
            "distraction_score",
        )
    )


def test_fusion_expiry_and_face_loss_remove_driver_features():
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=()))
    fusion.ingest(driver(phone_detected=True), received_time_ns=NOW)
    fresh = adapt_snapshot(fusion.snapshot(now_ns=NOW), now_ns=NOW)
    assert fresh.inputs["phone_usage"] is True
    assert fresh.inputs["distraction_score"] == pytest.approx(0.2)
    expired = adapt_snapshot(fusion.snapshot(now_ns=NOW + 401_000_000), now_ns=NOW + 401_000_000)
    assert expired.inputs["phone_usage"] is None
    absent = adapt_snapshot(
        snapshot(driver=driver(face_detected=False, phone_detected=True)), now_ns=NOW
    )
    assert all(value is None for value in absent.inputs.values())


def test_eye_placeholder_does_not_invent_drowsiness_and_audio_is_withheld():
    audio = AudioEvent(header(), AudioKind.SIREN, 0.99, 960, "audio-event-v1", "left")
    row = adapt_snapshot(
        snapshot(driver=driver(eyes_closed=True, microsleep_ms=2000), audio=audio), now_ns=NOW
    ).inputs
    assert row["driver_drowsy"] is False  # Derived solely from the supported score.
    assert row["siren_detected"] is row["horn_detected"] is row["audio_direction"] is None
    assert row["traffic_light"] is row["pedestrian_count"] is row["vehicle_count"] is None


@pytest.mark.parametrize(
    "api_valid,age_ms,expected", [(True, 0, "RAIN"), (False, 0, None), (True, 300001, None)]
)
def test_environment_requires_fresh_valid_api(api_valid, age_ms, expected):
    env = EnvironmentState(header(), weather=Weather.RAIN, is_night=True, api_valid=api_valid)
    row = adapt_snapshot(snapshot(environment=env), now_ns=NOW + age_ms * 1_000_000).inputs
    assert row["weather"] == expected
    assert row["is_night"] is (True if expected else None)


def test_slow_inference_is_nonblocking_and_results_cannot_outlive_inputs():
    entered, release = threading.Event(), threading.Event()

    class SlowPredictor:
        def predict(self, sample):
            entered.set()
            assert release.wait(3)
            return {"status": "ok", "xgboost_risk_level": "HIGH", "degraded": True}

    worker = ShadowRisk(SlowPredictor())
    snap = snapshot(driver=driver(phone_detected=True))
    try:
        assert worker.update(snap, now_ns=NOW)["status"] == "pending"
        assert entered.wait(1)
        # This call must finish while prediction is still blocked on release.
        expired = worker.update(snap, now_ns=NOW + 401_000_000)
        assert expired["status"] == "unavailable" and expired["xgboost_risk_level"] is None
        release.set()
        result = eventually(lambda: worker.update(snap, now_ns=NOW))
        assert result["xgboost_risk_level"] == "HIGH"
        assert result["mode"] == "display_only"
        assert worker.update(snapshot(), now_ns=NOW)["xgboost_risk_level"] is None
        changed = worker.update(snapshot(driver=driver()), now_ns=NOW)
        assert changed["status"] == "pending" and changed["xgboost_risk_level"] is None
    finally:
        release.set()
        worker.close()


def test_latest_pending_observation_replaces_backlog():
    entered, release = threading.Event(), threading.Event()
    seen = []

    class BlockFirst:
        def predict(self, row):
            seen.append(row["distraction_score"])
            if len(seen) == 1:
                entered.set()
                assert release.wait(3)
            return {"status": "ok", "xgboost_risk_level": "LOW"}

    worker = ShadowRisk(BlockFirst())
    try:
        worker.update(snapshot(driver=driver(attention_score=0.0)), now_ns=NOW)
        assert entered.wait(1)
        worker.update(snapshot(driver=driver(attention_score=0.25)), now_ns=NOW)
        newest = snapshot(driver=driver(attention_score=0.5))
        worker.update(newest, now_ns=NOW)
        release.set()
        result = eventually(lambda: worker.update(newest, now_ns=NOW))
        assert result["status"] == "ok"
        assert seen == [1.0, 0.5]
    finally:
        release.set()
        worker.close()


def test_worker_failure_is_explicit_and_never_normal():
    class Broken:
        def predict(self, row):
            raise RuntimeError("test failure")

    worker = ShadowRisk(Broken())
    try:
        result = eventually(lambda: worker.update(snapshot(driver=driver()), now_ns=NOW))
        assert result["status"] == "model_error"
        assert result["xgboost_risk_level"] is None and result["risk_probability"] is None
    finally:
        worker.close()


@pytest.fixture
def predictor():
    if not (MODEL / "metadata.json").exists():
        pytest.skip("Legacy B model was removed; v3 is tested separately")
    pytest.importorskip("xgboost")
    from codrive.risk_ml.infer import Predictor

    return Predictor(MODEL)


def test_real_model_and_template_with_partial_live_inputs(predictor):
    from codrive.risk_ml.features import RAW_FIELDS as MODEL_FIELDS

    assert list(RAW_FIELDS) == MODEL_FIELDS
    result = predictor.predict(
        adapt_snapshot(
            snapshot(
                driver=driver(
                    phone_detected=True,
                    looking_forward=False,
                    attention_score=0.2,
                )
            ),
            now_ns=NOW,
        ).inputs
    )
    assert result["status"] == "ok"
    assert result["xgboost_risk_level"] == "HIGH"
    assert sum(result["risk_probability"].values()) == pytest.approx(1)
    assert "휴대폰" in result["warning_message"] and "판단이 제한" in result["warning_message"]
    assert "SIREN" not in result["main_risk_factors"]
    with pytest.raises(ValueError, match="No usable"):
        predictor.predict(dict.fromkeys(RAW_FIELDS))


@pytest.mark.parametrize("damage", ["checksum", "contract", "hybrid"])
def test_incompatible_model_is_rejected(tmp_path, predictor, damage):
    from codrive.risk_ml.infer import Predictor

    folder = tmp_path / "model"
    shutil.copytree(MODEL, folder)
    if damage == "checksum":
        with (folder / "model.ubj").open("ab") as stream:
            stream.write(b"corrupt")
    else:
        path = folder / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["feature_contract_version" if damage == "contract" else "experiment"] = (
            1 if damage == "contract" else "C"
        )
        path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        Predictor(folder)
