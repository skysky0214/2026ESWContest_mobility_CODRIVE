from __future__ import annotations

import json
from typing import ClassVar

import pytest

from codrive.apps.dms import YoloDmsPublisher, run_camera_loop
from codrive.apps.local_bus import LocalBus as InMemoryBus
from codrive.fusion import SituationFusion
from codrive.risk import RiskEngine, RiskPolicy
from codrive.runtime import decode_model
from codrive.models import DriverState, Header, from_json, to_json
from codrive.hardware.camera import UsbCameraConfig
from codrive.sensor_input.driver.dms import (
    UltralyticsYoloDms,
    YoloDetection,
    YoloDmsEstimator,
    detections_from_ultralytics,
)

NOW_NS = 5_000_000_000_000


class _TensorLike:
    def __init__(self, values: object) -> None:
        self.values = values

    def cpu(self) -> _TensorLike:
        return self

    def tolist(self) -> object:
        return self.values


class _Boxes:
    cls = _TensorLike([0, 1, 3])
    conf = _TensorLike([0.92, 0.88, 0.74])
    xyxy = _TensorLike(
        [
            [10, 20, 200, 220],
            [50, 90, 170, 130],
            [220, 120, 300, 260],
        ]
    )


class _Result:
    names: ClassVar[dict[int, str]] = {0: "face", 1: "eyes_closed", 3: "phone"}
    boxes: ClassVar[_Boxes] = _Boxes()


class _FakeYolo:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def predict(self, frame: object, **kwargs: object) -> list[object]:
        self.calls.append({"frame": frame, **kwargs})
        return [self.result]


def test_ultralytics_detection_parser_keeps_names_confidence_and_boxes() -> None:
    detections = detections_from_ultralytics(_Result())

    assert detections == (
        YoloDetection("face", 0.92, (10.0, 20.0, 200.0, 220.0)),
        YoloDetection("eyes_closed", 0.88, (50.0, 90.0, 170.0, 130.0)),
        YoloDetection("phone", 0.74, (220.0, 120.0, 300.0, 260.0)),
    )


def test_dms_estimator_tracks_microsleep_and_attention() -> None:
    estimator = YoloDmsEstimator()
    detections = (
        YoloDetection("face", 0.9),
        YoloDetection("eyes_closed", 0.9),
        YoloDetection("yawn", 0.7),
    )

    first = estimator.observe(detections, capture_time_ns=NOW_NS)
    for step in range(1, 17):
        second = estimator.observe(detections, capture_time_ns=NOW_NS + step * 100_000_000)

    assert first.eyes_closed
    assert first.microsleep_ms == 0
    assert second.microsleep_ms == 1_600
    assert second.drowsiness_score >= 0.85
    assert second.attention_score < 0.8
    assert second.yawning


def test_dms_estimator_maps_alertai_head_drop_to_drowsiness() -> None:
    observation = YoloDmsEstimator().observe(
        (
            YoloDetection("Drowsy", 0.86),
            YoloDetection("Head drop", 0.9),
        ),
        capture_time_ns=NOW_NS,
    )

    assert observation.face_detected
    assert not observation.looking_forward
    assert observation.drowsiness_score >= 0.5
    assert observation.attention_score < 0.8


def test_yolo_dms_publisher_emits_driver_state() -> None:
    bus = InMemoryBus(clock_ns=lambda: NOW_NS)
    fake_yolo = _FakeYolo(_Result())
    dms = UltralyticsYoloDms("models/dms.engine", model=fake_yolo, half=False)
    publisher = YoloDmsPublisher(bus, dms)

    state = publisher.publish_frame(object(), capture_time_ns=NOW_NS)
    wire = bus.take_json("driver_state", max_samples=1)[0]

    assert state.header.source_id == "jetson-cabin.dms"
    assert state.camera_id == "cabin"
    assert state.eyes_closed
    assert state.phone_detected
    decoded = decode_model(wire, DriverState)
    assert decoded == state
    assert fake_yolo.calls[0]["imgsz"] == 640


class FakeCapture:
    def __init__(self, count=3, fps=10):
        self.count, self.fps, self.released = count, fps, False

    def get(self, prop):
        return self.fps

    def read(self):
        if self.count <= 0:
            return False, None
        self.count -= 1
        return True, object()

    def release(self):
        self.released = True


def test_replay_uses_video_clock_and_releases_at_eof():
    bus = InMemoryBus()
    capture = FakeCapture()
    dms = UltralyticsYoloDms("unused.engine", model=_FakeYolo(_Result()), half=False)
    assert (
        run_camera_loop(
            bus=bus, dms=dms, camera=UsbCameraConfig(), video="injected", capture=capture
        )
        == 3
    )
    states = [decode_model(item, DriverState) for item in bus.take_json("driver_state", 16)]
    assert [item.microsleep_ms for item in states] == [0, 100, 200]
    assert states[2].header.capture_time_ns - states[0].header.capture_time_ns == 200_000_000
    assert capture.released
    assert dms.model.calls[0]["half"] is False


def test_failed_camera_stops_and_marks_health():
    bus, capture = InMemoryBus(), FakeCapture(count=0)
    dms = UltralyticsYoloDms("unused.engine", model=_FakeYolo(_Result()))
    with pytest.raises(RuntimeError, match="repeatedly"):
        run_camera_loop(
            bus=bus, dms=dms, camera=UsbCameraConfig(), capture=capture, max_read_failures=1
        )
    assert capture.released
    health = json.loads(bus.take_json("node_health", 16)[-1].payload_json)
    assert health["state"] == "degraded"
    assert health["detail"] == "dms_stopped"


def test_estimator_does_not_bridge_missing_frames_or_clock_reset():
    estimator = YoloDmsEstimator()
    closed = (YoloDetection("eyes_closed", 0.9),)
    estimator.observe(closed, capture_time_ns=NOW_NS)
    assert estimator.observe(closed, capture_time_ns=NOW_NS + 2_000_000_000).microsleep_ms == 0
    assert estimator.observe(closed, capture_time_ns=NOW_NS).microsleep_ms == 0


def test_dms_contract_backward_defaults_and_new_fields():
    state = DriverState(
        Header.now("jetson-cabin.dms", 1),
        "cabin",
        "test",
        0.5,
        False,
        microsleep_ms=1200,
        yawning=True,
    )
    assert from_json(DriverState, to_json(state)) == state
    payload = json.loads(to_json(state))
    del payload["microsleep_ms"], payload["yawning"]
    assert from_json(DriverState, json.dumps(payload)).microsleep_ms == 0
    with pytest.raises(TypeError, match="boolean"):
        DriverState(state.header, "cabin", "test", 1, "yes")


def test_face_missing_is_degraded_without_false_drowsiness():
    state = DriverState(
        Header.now("jetson-cabin.dms", 1, capture_time_ns=NOW_NS),
        "cabin",
        "test",
        0.5,
        False,
        face_detected=False,
    )
    pipeline = SituationFusion()
    pipeline.ingest(state, received_time_ns=NOW_NS)
    snapshot = pipeline.snapshot(now_ns=NOW_NS)
    assert "driver:face_missing" in snapshot.degraded_reasons
    assert RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(snapshot, now_ns=NOW_NS) is None


def test_dms_state_reaches_risk_pipeline():
    publisher = YoloDmsPublisher(
        InMemoryBus(), UltralyticsYoloDms("unused.engine", model=_FakeYolo(_Result()))
    )
    pipeline = SituationFusion()
    for i in range(20):
        now = NOW_NS + i * 100_000_000
        state = publisher.publish_frame(object(), capture_time_ns=now)
        pipeline.ingest(state, received_time_ns=now)
        snapshot = pipeline.snapshot(now_ns=now)
    risk = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(snapshot, now_ns=now)
    assert risk is not None
    assert risk.code == "driver_phone_observation"
    assert risk.severity.name == "INFO"


def test_model_missing_and_generic_classes_fail_explicitly(tmp_path):
    dms = UltralyticsYoloDms(tmp_path / "absent.pt")
    with pytest.raises(FileNotFoundError):
        _ = dms.model
    with pytest.raises(ValueError, match="DMS"):
        dms.validate_classes({0: "person", 1: "car", 2: "cell phone"})
    with pytest.raises(ValueError, match="finite"):
        YoloDetection("face", 0.9, (float("nan"), 0, 1, 1))


@pytest.mark.parametrize(
    "label,expected",
    [
        ("awake", "forward"),
        ("distracted", "away"),
        ("drowsy", "drowsy"),
        ("head drop", "drowsy"),
        ("phone", "phone"),
        ("smoking", "away"),
        ("yawn", "yawn"),
    ],
)
def test_checked_in_model_labels_are_usable(label, expected):
    from pathlib import Path

    from codrive.config import load_config
    from codrive.sensor_input.driver.dms import YoloDmsPolicy

    config = load_config(Path(__file__).resolve().parents[2] / "config/board/jetson-cabin.yaml")
    mapping = config["cameras"][0]["pipelines"][0]["policy"]
    policy = YoloDmsPolicy(**{key: tuple(value) for key, value in mapping.items()})
    observation = YoloDmsEstimator(policy).observe(
        (YoloDetection(label, 0.95),), capture_time_ns=NOW_NS
    )
    assert observation.face_detected  # Driver-presence proxy for this behavior-only model.
    assert not observation.eyes_closed  # Drowsy does not prove eyelid closure.
    if expected == "forward":
        assert observation.looking_forward
    if expected == "away":
        assert not observation.looking_forward
    if expected == "drowsy":
        assert observation.drowsiness_score > 0.5
    if expected == "phone":
        assert observation.phone_detected
    if expected == "yawn":
        assert observation.yawning


@pytest.mark.parametrize("label,expected", [
    ("awake", "awake"), ("distracted", "distracted"), ("drowsy", "drowsy"),
    ("head_drop", "head_drop"), ("phone", "phone"), ("smoking", None), ("yawn", None),
])
def test_deployed_behavior_mapping(label, expected):
    from pathlib import Path
    from codrive.config import load_config
    from codrive.sensor_input.driver.dms import YoloDmsPolicy

    config = load_config(Path(__file__).resolve().parents[2] / "config/integration/jetson-cabin.yaml")
    mapping = config["cameras"][0]["pipelines"][0]["policy"]
    policy = YoloDmsPolicy(**{k: tuple(v) if k.endswith("_classes") else v for k, v in mapping.items()})
    estimator = YoloDmsEstimator(policy)
    observation = estimator.observe((YoloDetection(label, .95),), capture_time_ns=NOW_NS)
    assert observation.behavior == expected
    assert observation.face_detected == (expected is not None)
    assert observation.looking_forward == (expected == "awake")
    assert not observation.yawning
    if label == "phone":
        rejected = estimator.observe((YoloDetection(label, .54),), capture_time_ns=NOW_NS + 100_000_000)
        assert rejected.behavior is None
        assert not rejected.face_detected
