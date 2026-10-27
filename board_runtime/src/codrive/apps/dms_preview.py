"""Local OpenCV preview for the YOLO DMS pipeline."""

from __future__ import annotations

from typing import Any

from ..sensor_input.driver.dms import DriverDmsObservation, YoloDetection


def _box_color(label: str) -> tuple[int, int, int]:
    normalized = label.lower().replace(" ", "_").replace("-", "_")
    if normalized in {"drowsy", "sleep", "sleepy", "head_drop", "head_down"}:
        return (0, 0, 255)
    if normalized in {"yawn", "yawning", "mouth_open"}:
        return (0, 165, 255)
    if normalized in {"phone", "cell_phone", "mobile_phone", "distracted"}:
        return (255, 0, 255)
    if normalized in {"eyes_closed", "eye_closed", "closed_eye"}:
        return (0, 0, 255)
    return (0, 200, 0)


def _clamp_box(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return (
        max(0, min(width - 1, round(x1))),
        max(0, min(height - 1, round(y1))),
        max(0, min(width - 1, round(x2))),
        max(0, min(height - 1, round(y2))),
    )


def draw_dms_overlay(
    cv2: Any,
    frame: Any,
    detections: tuple[YoloDetection, ...],
    observation: DriverDmsObservation,
    *,
    inference_ms: float,
    phone_threshold: float | None = None,
    status_below: bool = False,
) -> Any:
    """Draw model detections and the derived driver state on a frame."""

    output = frame.copy() if hasattr(frame, "copy") else frame
    height, width = output.shape[:2]
    raw_phone = max((d.confidence for d in detections
                     if d.class_name.lower().replace(" ", "_") in {"phone", "cell_phone", "mobile_phone"}), default=0.0)
    extra = (f"phone_gate={phone_threshold:.2f} raw_phone_max={raw_phone:.2f}",) if phone_threshold is not None else ()
    panel_height = 24 + (4 + len(extra) + (observation.behavior is not None)) * 24
    if status_below:
        output = cv2.copyMakeBorder(output, 0, panel_height, 0, 0, cv2.BORDER_CONSTANT, value=(0, 0, 0))

    for detection in detections:
        if detection.bbox_xyxy is None:
            continue
        x1, y1, x2, y2 = _clamp_box(detection.bbox_xyxy, width, height)
        color = _box_color(detection.class_name)
        label = f"{detection.class_name} {detection.confidence:.2f}"
        if phone_threshold is not None and detection.class_name.lower().replace(" ", "_") in {"phone", "cell_phone", "mobile_phone"}:
            accepted = detection.confidence >= phone_threshold
            label += " PASS" if accepted else " below"
            if not accepted:
                color = (150, 150, 150)
        cv2.rectangle(output, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            output,
            label,
            (x1, max(18, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )

    status_color = (0, 0, 255) if observation.drowsiness_score >= 0.65 else (0, 200, 0)
    lines = (
        f"face={observation.face_detected} forward={observation.looking_forward}",
        f"attention={observation.attention_score:.2f} drowsy={observation.drowsiness_score:.2f}",
        f"eyes_closed={observation.eyes_closed} microsleep={observation.microsleep_ms}ms",
        f"yawn={observation.yawning} phone={observation.phone_detected} infer={inference_ms:.1f}ms",
    ) + ((f"behavior={observation.behavior} conf={(observation.behavior_confidence or 0.0):.2f}",)
         if observation.behavior is not None else ()) + extra
    panel_y = height if status_below else 0
    cv2.rectangle(output, (0, panel_y), (width - 1, panel_y + panel_height), (0, 0, 0), -1)
    for index, line in enumerate(lines):
        cv2.putText(
            output,
            line,
            (12, panel_y + 26 + index * 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            status_color if index == 1 else (230, 230, 230),
            2,
            cv2.LINE_AA,
        )
    return output


def draw_state_overlay(cv2, frame, dms, state):
    """Annotate the exact detections used for this frame's published DriverState."""
    observation = DriverDmsObservation(
        state.attention_score, state.looking_forward, state.drowsiness_score,
        state.microsleep_ms, state.eyes_closed, state.yawning, state.phone_detected, state.face_detected,
        behavior=state.behavior, behavior_confidence=state.behavior_confidence,
    )
    return draw_dms_overlay(cv2, frame, dms.last_detections, observation,
                            inference_ms=state.inference_ms,
                            phone_threshold=dms.policy.phone_min_confidence, status_below=True)


def run_preview_loop(
    *,
    dms,
    camera,
    source_id="jetson-cabin.dms",
    camera_id="cabin",
    frame_id="camera_cabin_optical",
    max_frames=None,
    print_json=False,
    mirror=False,
    video=None,
):
    from .dms import run_camera_loop
    from .local_bus import LocalBus

    bus = LocalBus()
    try:
        return run_camera_loop(
            bus=bus,
            dms=dms,
            camera=camera,
            source_id=source_id,
            camera_id=camera_id,
            frame_id=frame_id,
            max_frames=max_frames,
            print_json=print_json,
            mirror=mirror,
            video=video,
            preview=True,
        )
    finally:
        bus.close()


def main(argv=None):
    import sys

    from .dms import main as run

    args = list(sys.argv[1:] if argv is None else argv)
    return run([*args, "--preview", "--dry-run"])


if __name__ == "__main__":
    raise SystemExit(main())
