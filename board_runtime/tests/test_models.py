from __future__ import annotations

from codrive.models import (
    BoundingBox,
    ExternalPerception,
    Header,
    TrackedObject,
    TrafficLight,
    from_json,
    to_json,
)
import json


def test_nested_model_json_round_trip() -> None:
    sample = ExternalPerception(
        header=Header.now("jetson-front", 7, frame_id="front"),
        camera_id="front",
        model_version="yolo11n-test",
        objects=(
            TrackedObject(
                track_id=3,
                class_name="pedestrian",
                confidence=0.93,
                bbox=BoundingBox(0.2, 0.1, 0.4, 0.8),
                distance_m=8.0,
                ttc_s=1.2,
            ),
        ),
        traffic_light=TrafficLight.RED,
        traffic_light_confidence=0.88,
    )

    decoded = from_json(ExternalPerception, to_json(sample))

    assert decoded == sample
    assert isinstance(decoded.objects, tuple)
    assert decoded.traffic_light is TrafficLight.RED


def test_bounding_box_rejects_pixel_coordinates() -> None:
    try:
        BoundingBox(1.0, 2.0, 20.0, 30.0)
    except ValueError as exc:
        assert "normalized" in str(exc)
    else:
        raise AssertionError("invalid box accepted")


def test_wire_primitive_types_and_confidence_ranges_are_checked() -> None:
    sample = ExternalPerception(
        header=Header.now("jetson-front.front", 1),
        camera_id="front",
        model_version="test",
    )
    payload = json.loads(to_json(sample))
    payload["lane_confidence"] = "high"
    try:
        from_json(ExternalPerception, json.dumps(payload))
    except TypeError as exc:
        assert "number" in str(exc)
    else:
        raise AssertionError("string confidence accepted")

    payload["lane_confidence"] = 1.5
    try:
        from_json(ExternalPerception, json.dumps(payload))
    except ValueError as exc:
        assert "<= 1.0" in str(exc)
    else:
        raise AssertionError("out-of-range confidence accepted")
