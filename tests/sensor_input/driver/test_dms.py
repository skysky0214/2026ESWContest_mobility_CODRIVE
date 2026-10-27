from __future__ import annotations

from typing import ClassVar

from codrive.sensor_input.driver.dms import (
    UltralyticsYoloDms,
    YoloDetection,
    YoloDmsEstimator,
    YoloDmsPublisher,
    detections_from_ultralytics,
)
from codrive.contracts.models import DriverState
from codrive.transport.model_bus import decode_model
from codrive.transport import InMemoryBus

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
    second = estimator.observe(detections, capture_time_ns=NOW_NS + 1_600_000_000)

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
