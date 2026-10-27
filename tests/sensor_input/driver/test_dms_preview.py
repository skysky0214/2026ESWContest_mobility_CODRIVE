from __future__ import annotations

from types import SimpleNamespace

from codrive.sensor_input.driver.dms import DriverDmsObservation, YoloDetection
from codrive.sensor_input.driver.dms_preview import draw_dms_overlay


class _FakeFrame:
    shape = (480, 640, 3)

    def __init__(self) -> None:
        self.copied = False

    def copy(self) -> _FakeFrame:
        clone = _FakeFrame()
        clone.copied = True
        return clone


def test_draw_dms_overlay_renders_boxes_and_status_lines() -> None:
    rectangles: list[tuple[object, object, object, object, int]] = []
    texts: list[str] = []
    fake_cv2 = SimpleNamespace(
        FONT_HERSHEY_SIMPLEX=0,
        LINE_AA=16,
        rectangle=lambda image, p1, p2, color, thickness: rectangles.append(
            (image, p1, p2, color, thickness)
        ),
        putText=lambda image, text, origin, font, scale, color, thickness, line: texts.append(
            text
        ),
    )

    output = draw_dms_overlay(
        fake_cv2,
        _FakeFrame(),
        (
            YoloDetection("Drowsy", 0.88, (10, 20, 120, 160)),
            YoloDetection("Phone", 0.75, (200, 30, 260, 110)),
        ),
        DriverDmsObservation(
            attention_score=0.3,
            looking_forward=False,
            drowsiness_score=0.8,
            microsleep_ms=1200,
            eyes_closed=True,
            yawning=False,
            phone_detected=True,
            face_detected=True,
        ),
        inference_ms=12.5,
    )

    assert output.copied
    assert len(rectangles) == 3
    assert "Drowsy 0.88" in texts
    assert "Phone 0.75" in texts
    assert any("attention=0.30 drowsy=0.80" in text for text in texts)
