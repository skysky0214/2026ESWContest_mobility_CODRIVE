"""YOLO-based driver monitoring pipeline for the Jetson cabin camera."""

from __future__ import annotations

import argparse
import importlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from ...hardware.camera import UsbCameraConfig, open_opencv_camera
from ...contracts.models import DriverState, Header, to_json
from ...transport.model_bus import JsonBus, publish_model
from ...transport import CycloneDdsBus, InMemoryBus


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_label(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _as_list(value: Any) -> list[Any]:
    for method in ("detach", "cpu"):
        candidate = getattr(value, method, None)
        if callable(candidate):
            value = candidate()
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        converted = tolist()
        return converted if isinstance(converted, list) else [converted]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    return [value]


@dataclass(frozen=True, slots=True)
class YoloDetection:
    class_name: str
    confidence: float
    bbox_xyxy: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.class_name, str) or not self.class_name.strip():
            raise ValueError("class_name must be non-empty")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be in [0, 1]")
        if self.bbox_xyxy is None:
            return
        if len(self.bbox_xyxy) != 4:
            raise ValueError("bbox_xyxy must contain four coordinates")
        x1, y1, x2, y2 = self.bbox_xyxy
        if x2 < x1 or y2 < y1:
            raise ValueError("bbox_xyxy must be ordered xyxy coordinates")

    @property
    def label(self) -> str:
        return _normalize_label(self.class_name)


@dataclass(frozen=True, slots=True)
class YoloDmsPolicy:
    face_classes: tuple[str, ...] = ("face", "driver_face")
    eyes_closed_classes: tuple[str, ...] = (
        "eyes_closed",
        "eye_closed",
        "closed_eye",
        "drowsy",
        "sleep",
        "sleepy",
    )
    eyes_open_classes: tuple[str, ...] = ("eyes_open", "eye_open", "open_eye")
    drowsy_classes: tuple[str, ...] = ("drowsy", "sleep", "sleepy", "head_drop", "head_down")
    yawn_classes: tuple[str, ...] = ("yawn", "yawning", "mouth_open")
    phone_classes: tuple[str, ...] = ("phone", "cell_phone", "mobile_phone")
    forward_classes: tuple[str, ...] = ("looking_forward", "forward")
    away_classes: tuple[str, ...] = (
        "looking_away",
        "looking_left",
        "looking_right",
        "looking_down",
        "head_drop",
        "head_down",
        "distracted",
    )
    min_confidence: float = 0.35
    face_min_confidence: float = 0.35
    eyes_closed_min_confidence: float = 0.45
    yawn_min_confidence: float = 0.45
    phone_min_confidence: float = 0.45
    away_min_confidence: float = 0.45
    microsleep_warning_ms: int = 800
    microsleep_critical_ms: int = 1_500
    smoothing_alpha: float = 0.6

    def __post_init__(self) -> None:
        for name in (
            "min_confidence",
            "face_min_confidence",
            "eyes_closed_min_confidence",
            "yawn_min_confidence",
            "phone_min_confidence",
            "away_min_confidence",
            "smoothing_alpha",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be in [0, 1]")
        for name in ("microsleep_warning_ms", "microsleep_critical_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def labels(self, classes: tuple[str, ...]) -> set[str]:
        return {_normalize_label(item) for item in classes}


@dataclass(frozen=True, slots=True)
class DriverDmsObservation:
    attention_score: float
    looking_forward: bool
    drowsiness_score: float
    microsleep_ms: int
    eyes_closed: bool
    yawning: bool
    phone_detected: bool
    face_detected: bool


def detections_from_ultralytics(result: Any) -> tuple[YoloDetection, ...]:
    """Convert one Ultralytics result object into stable detection records."""

    boxes = getattr(result, "boxes", None)
    if boxes is None:
        return ()
    names = getattr(result, "names", {})
    classes = _as_list(getattr(boxes, "cls", []))
    confidences = _as_list(getattr(boxes, "conf", []))
    boxes_xyxy = _as_list(getattr(boxes, "xyxy", []))

    detections: list[YoloDetection] = []
    for class_id, confidence, bbox in zip(classes, confidences, boxes_xyxy):
        class_index = int(class_id)
        name = names.get(class_index, str(class_index)) if isinstance(names, dict) else str(class_index)
        confidence_float = float(confidence)
        bbox_values = _as_list(bbox)
        bbox_tuple = tuple(float(item) for item in bbox_values[:4])
        detections.append(YoloDetection(str(name), confidence_float, bbox_tuple))
    return tuple(detections)


@dataclass(slots=True)
class YoloDmsEstimator:
    policy: YoloDmsPolicy = field(default_factory=YoloDmsPolicy)
    _eyes_closed_since_ns: int | None = None
    _smoothed_drowsiness: float = 0.0

    def _max_confidence(
        self, detections: tuple[YoloDetection, ...], labels: set[str], threshold: float
    ) -> float:
        return max(
            (
                detection.confidence
                for detection in detections
                if detection.label in labels and detection.confidence >= threshold
            ),
            default=0.0,
        )

    def observe(
        self, detections: tuple[YoloDetection, ...], *, capture_time_ns: int
    ) -> DriverDmsObservation:
        policy = self.policy
        usable = tuple(
            detection for detection in detections if detection.confidence >= policy.min_confidence
        )
        face_conf = self._max_confidence(
            usable, policy.labels(policy.face_classes), policy.face_min_confidence
        )
        closed_conf = self._max_confidence(
            usable, policy.labels(policy.eyes_closed_classes), policy.eyes_closed_min_confidence
        )
        open_conf = self._max_confidence(
            usable, policy.labels(policy.eyes_open_classes), policy.min_confidence
        )
        yawn_conf = self._max_confidence(
            usable, policy.labels(policy.yawn_classes), policy.yawn_min_confidence
        )
        drowsy_conf = self._max_confidence(
            usable, policy.labels(policy.drowsy_classes), policy.eyes_closed_min_confidence
        )
        phone_conf = self._max_confidence(
            usable, policy.labels(policy.phone_classes), policy.phone_min_confidence
        )
        forward_conf = self._max_confidence(
            usable, policy.labels(policy.forward_classes), policy.min_confidence
        )
        away_conf = self._max_confidence(
            usable, policy.labels(policy.away_classes), policy.away_min_confidence
        )

        face_detected = face_conf > 0.0 or any(
            confidence > 0.0
            for confidence in (closed_conf, open_conf, drowsy_conf, yawn_conf, forward_conf)
        )
        eyes_closed = closed_conf > 0.0 and closed_conf >= open_conf
        yawning = yawn_conf > 0.0
        phone_detected = phone_conf > 0.0
        looking_forward = face_detected and away_conf == 0.0 and (
            forward_conf > 0.0 or not policy.labels(policy.forward_classes)
        )
        if face_detected and forward_conf == 0.0 and away_conf == 0.0:
            looking_forward = True

        if eyes_closed:
            if self._eyes_closed_since_ns is None:
                self._eyes_closed_since_ns = capture_time_ns
            microsleep_ms = max(0, int((capture_time_ns - self._eyes_closed_since_ns) / 1_000_000))
        else:
            self._eyes_closed_since_ns = None
            microsleep_ms = 0

        microsleep_score = _clamp01(microsleep_ms / max(policy.microsleep_critical_ms, 1))
        raw_drowsiness = max(closed_conf, drowsy_conf, yawn_conf * 0.75, microsleep_score)
        alpha = policy.smoothing_alpha
        self._smoothed_drowsiness = _clamp01(
            alpha * raw_drowsiness + (1.0 - alpha) * self._smoothed_drowsiness
        )
        self._smoothed_drowsiness = max(self._smoothed_drowsiness, microsleep_score)
        if not face_detected:
            self._smoothed_drowsiness = 0.0

        distraction_penalty = max(
            away_conf,
            phone_conf,
            0.45 if not face_detected else 0.0,
            closed_conf * 0.35,
        )
        attention_score = _clamp01(1.0 - distraction_penalty)

        return DriverDmsObservation(
            attention_score=attention_score,
            looking_forward=looking_forward,
            drowsiness_score=self._smoothed_drowsiness,
            microsleep_ms=microsleep_ms,
            eyes_closed=eyes_closed,
            yawning=yawning,
            phone_detected=phone_detected,
            face_detected=face_detected,
        )


class UltralyticsYoloDms:
    """Lazy Ultralytics YOLO wrapper that emits CODRIVE DriverState objects."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        model: Any | None = None,
        model_version: str | None = None,
        policy: YoloDmsPolicy | None = None,
        device: str | int | None = None,
        imgsz: int = 640,
        half: bool = True,
        verbose: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.model_version = model_version or self.model_path.name
        self.policy = policy or YoloDmsPolicy()
        self.estimator = YoloDmsEstimator(self.policy)
        self.device = device
        self.imgsz = imgsz
        self.half = half
        self.verbose = verbose
        self._model = model

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                ultralytics = importlib.import_module("ultralytics")
            except ImportError as exc:
                raise RuntimeError("ultralytics is required for YOLO DMS inference") from exc
            self._model = ultralytics.YOLO(str(self.model_path))
        return self._model

    def detections(self, frame: Any) -> tuple[YoloDetection, ...]:
        results = self.model.predict(frame, imgsz=self.imgsz, device=self.device, verbose=self.verbose)
        if not results:
            return ()
        return detections_from_ultralytics(results[0])

    def state_from_frame(
        self,
        frame: Any,
        *,
        header: Header,
        camera_id: str,
    ) -> DriverState:
        started_ns = time.perf_counter_ns()
        observation = self.estimator.observe(
            self.detections(frame),
            capture_time_ns=header.capture_time_ns,
        )
        inference_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        return DriverState(
            header=header,
            camera_id=camera_id,
            model_version=self.model_version,
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


@dataclass(slots=True)
class YoloDmsPublisher:
    bus: JsonBus
    dms: UltralyticsYoloDms
    source_id: str = "jetson-cabin.dms"
    camera_id: str = "cabin"
    frame_id: str = "camera_cabin_optical"
    sequence: int = 0

    def publish_frame(self, frame: Any, *, capture_time_ns: int | None = None) -> DriverState:
        self.sequence += 1
        header = Header.now(
            self.source_id,
            self.sequence,
            frame_id=self.frame_id,
            capture_time_ns=capture_time_ns,
        )
        state = self.dms.state_from_frame(frame, header=header, camera_id=self.camera_id)
        publish_model(self.bus, "driver_state", state)
        return state


def run_camera_loop(
    *,
    bus: JsonBus,
    dms: UltralyticsYoloDms,
    camera: UsbCameraConfig,
    source_id: str = "jetson-cabin.dms",
    camera_id: str = "cabin",
    frame_id: str = "camera_cabin_optical",
    rate_hz: float = 15.0,
    max_frames: int | None = None,
    stop: Event | None = None,
    print_json: bool = False,
) -> int:
    if rate_hz <= 0.0:
        raise ValueError("rate_hz must be positive")
    if max_frames is not None and max_frames < 1:
        raise ValueError("max_frames must be positive when provided")

    stop_event = Event() if stop is None else stop
    publisher = YoloDmsPublisher(
        bus,
        dms,
        source_id=source_id,
        camera_id=camera_id,
        frame_id=frame_id,
    )
    capture = open_opencv_camera(camera)
    period_s = 1.0 / rate_hz
    next_tick = time.monotonic()
    published = 0
    try:
        while not stop_event.is_set() and (max_frames is None or published < max_frames):
            capture_time_ns = time.time_ns()
            ok, frame = capture.read()
            if ok:
                state = publisher.publish_frame(frame, capture_time_ns=capture_time_ns)
                published += 1
                if print_json:
                    print(to_json(state), flush=True)
            next_tick += period_s
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                stop_event.wait(remaining)
            else:
                next_tick = time.monotonic()
    finally:
        capture.release()
    return published


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run YOLO DMS and publish DriverState.")
    parser.add_argument("--model", default="models/dms.engine", help="YOLO .engine/.pt/.onnx path")
    parser.add_argument("--domain-id", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory bus instead of DDS")
    parser.add_argument("--print-json", action="store_true", help="Print each DriverState JSON")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--source-id", default="jetson-cabin.dms")
    parser.add_argument("--camera-id", default="cabin")
    parser.add_argument("--frame-id", default="camera_cabin_optical")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--input-format", choices=("MJPG", "YUY2"), default="MJPG")
    parser.add_argument("--no-gstreamer", action="store_true", help="Open USB camera directly")
    parser.add_argument("--rate-hz", type=float, default=15.0)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default=None, help="Ultralytics device, e.g. 0, cuda:0, cpu")
    parser.add_argument("--no-half", action="store_true", help="Disable FP16 inference request")
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
    dms = UltralyticsYoloDms(
        args.model,
        device=args.device,
        imgsz=args.imgsz,
        half=not args.no_half,
    )
    bus: JsonBus = InMemoryBus() if args.dry_run else CycloneDdsBus(domain_id=args.domain_id)
    try:
        run_camera_loop(
            bus=bus,
            dms=dms,
            camera=camera,
            source_id=args.source_id,
            camera_id=args.camera_id,
            frame_id=args.frame_id,
            rate_hz=args.rate_hz,
            max_frames=args.max_frames,
            print_json=args.print_json,
        )
    except KeyboardInterrupt:
        return 130
    finally:
        close = getattr(bus, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
