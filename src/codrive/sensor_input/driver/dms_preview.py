"""Local OpenCV preview for the YOLO DMS pipeline."""

from __future__ import annotations

import argparse
import importlib
import time
from typing import Any

from .dms import DriverDmsObservation, UltralyticsYoloDms, YoloDetection
from ...hardware.camera import UsbCameraConfig, open_opencv_camera
from ...contracts.models import DriverState, Header, to_json


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
) -> Any:
    """Draw model detections and the derived driver state on a frame."""

    output = frame.copy() if hasattr(frame, "copy") else frame
    height, width = output.shape[:2]

    for detection in detections:
        if detection.bbox_xyxy is None:
            continue
        x1, y1, x2, y2 = _clamp_box(detection.bbox_xyxy, width, height)
        color = _box_color(detection.class_name)
        label = f"{detection.class_name} {detection.confidence:.2f}"
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
    )
    panel_height = 24 + len(lines) * 24
    cv2.rectangle(output, (0, 0), (min(width - 1, 520), panel_height), (0, 0, 0), -1)
    for index, line in enumerate(lines):
        cv2.putText(
            output,
            line,
            (12, 26 + index * 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            status_color if index == 1 else (230, 230, 230),
            2,
            cv2.LINE_AA,
        )
    return output


def _state_from_observation(
    observation: DriverDmsObservation,
    *,
    header: Header,
    camera_id: str,
    model_version: str,
    inference_ms: float,
) -> DriverState:
    return DriverState(
        header=header,
        camera_id=camera_id,
        model_version=model_version,
        attention_score=observation.attention_score,
        looking_forward=observation.looking_forward,
        drowsiness_score=observation.drowsiness_score,
        microsleep_ms=observation.microsleep_ms,
        eyes_closed=observation.eyes_closed,
        yawning=observation.yawning,
        phone_detected=observation.phone_detected,
        face_detected=observation.face_detected,
        inference_ms=inference_ms,
    )


def run_preview_loop(
    *,
    dms: UltralyticsYoloDms,
    camera: UsbCameraConfig,
    source_id: str = "jetson-cabin.dms-preview",
    camera_id: str = "cabin",
    frame_id: str = "camera_cabin_optical",
    window_title: str = "CODRIVE YOLO DMS Preview",
    max_frames: int | None = None,
    print_json: bool = False,
    mirror: bool = False,
) -> int:
    if max_frames is not None and max_frames < 1:
        raise ValueError("max_frames must be positive when provided")
    try:
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for DMS preview") from exc

    capture = open_opencv_camera(camera)
    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    frames = 0
    try:
        while max_frames is None or frames < max_frames:
            capture_time_ns = time.time_ns()
            ok, frame = capture.read()
            if not ok:
                continue
            if mirror:
                frame = cv2.flip(frame, 1)

            started_ns = time.perf_counter_ns()
            detections = dms.detections(frame)
            observation = dms.estimator.observe(detections, capture_time_ns=capture_time_ns)
            inference_ms = (time.perf_counter_ns() - started_ns) / 1_000_000

            if print_json:
                state = _state_from_observation(
                    observation,
                    header=Header.now(
                        source_id,
                        frames + 1,
                        frame_id=frame_id,
                        capture_time_ns=capture_time_ns,
                    ),
                    camera_id=camera_id,
                    model_version=dms.model_version,
                    inference_ms=inference_ms,
                )
                print(to_json(state), flush=True)

            cv2.imshow(
                window_title,
                draw_dms_overlay(
                    cv2, frame, detections, observation, inference_ms=inference_ms
                ),
            )
            frames += 1
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        capture.release()
        cv2.destroyWindow(window_title)
    return frames


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview YOLO DMS detections with OpenCV.")
    parser.add_argument("--model", default="models/dms.pt", help="YOLO .pt/.engine/.onnx path")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--source-id", default="jetson-cabin.dms-preview")
    parser.add_argument("--camera-id", default="cabin")
    parser.add_argument("--frame-id", default="camera_cabin_optical")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--input-format", choices=("MJPG", "YUY2"), default="MJPG")
    parser.add_argument("--no-gstreamer", action="store_true")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default=None, help="Ultralytics device, e.g. 0, cuda:0, cpu")
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--window-title", default="CODRIVE YOLO DMS Preview")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    camera = UsbCameraConfig(
        device_index=args.device_index,
        width=args.width,
        height=args.height,
        fps=args.fps,
        input_format=args.input_format,
        use_gstreamer=not args.no_gstreamer,
    )
    dms = UltralyticsYoloDms(args.model, device=args.device, imgsz=args.imgsz)
    try:
        run_preview_loop(
            dms=dms,
            camera=camera,
            source_id=args.source_id,
            camera_id=args.camera_id,
            frame_id=args.frame_id,
            window_title=args.window_title,
            max_frames=args.max_frames,
            print_json=args.print_json,
            mirror=args.mirror,
        )
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
