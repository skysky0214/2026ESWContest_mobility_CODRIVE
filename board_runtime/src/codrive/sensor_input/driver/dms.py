"""YOLO-based driver monitoring pipeline for the Jetson cabin camera."""

from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ...models import DriverState, Header


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
        if any(
            not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
            for x in self.bbox_xyxy
        ):
            raise ValueError("bbox coordinates must be finite numbers")
        x1, y1, x2, y2 = self.bbox_xyxy
        if x2 < x1 or y2 < y1:
            raise ValueError("bbox_xyxy must be ordered xyxy coordinates")

    @property
    def label(self) -> str:
        return _normalize_label(self.class_name)


@dataclass(frozen=True, slots=True)
class YoloDmsPolicy:
    ignored_classes: tuple[str, ...] = ()
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
    max_frame_gap_ms: int = 400

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
        for name in ("microsleep_warning_ms", "microsleep_critical_ms", "max_frame_gap_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_frame_gap_ms == 0:
            raise ValueError("max_frame_gap_ms must be positive")
        if self.microsleep_critical_ms < self.microsleep_warning_ms:
            raise ValueError("critical microsleep duration must be >= warning duration")
        for name in (
            "ignored_classes",
            "face_classes",
            "eyes_closed_classes",
            "eyes_open_classes",
            "drowsy_classes",
            "yawn_classes",
            "phone_classes",
            "forward_classes",
            "away_classes",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(x, str) or not x.strip() for x in values
            ):
                raise ValueError(f"{name} must contain non-empty class names")

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
    behavior: str | None = None
    behavior_confidence: float | None = None


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
        name = (
            names.get(class_index, str(class_index))
            if isinstance(names, dict)
            else names[class_index]
            if isinstance(names, (list, tuple)) and 0 <= class_index < len(names)
            else str(class_index)
        )
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
    _last_capture_ns: int | None = None

    def reset(self) -> None:
        self._eyes_closed_since_ns = None
        self._smoothed_drowsiness = 0.0
        self._last_capture_ns = None

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
        if self._last_capture_ns is not None and (
            capture_time_ns < self._last_capture_ns
            or capture_time_ns - self._last_capture_ns > self.policy.max_frame_gap_ms * 1_000_000
        ):
            self.reset()
        self._last_capture_ns = capture_time_ns
        policy = self.policy
        usable = tuple(
            detection for detection in detections if detection.confidence >= policy.min_confidence
            and detection.label not in policy.labels(policy.ignored_classes)
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
            for confidence in (closed_conf, open_conf, drowsy_conf, yawn_conf, forward_conf, away_conf, phone_conf)
        )
        head_drop_conf = self._max_confidence(
            usable, policy.labels(policy.drowsy_classes) & {"head_drop", "head_down"},
            policy.eyes_closed_min_confidence,
        )
        behavior, behavior_confidence = next(
            ((name, confidence) for name, confidence in (
                ("phone", phone_conf), ("head_drop", head_drop_conf), ("drowsy", drowsy_conf),
                ("distracted", away_conf), ("awake", forward_conf),
            ) if confidence > 0), (None, None),
        )
        eyes_closed = closed_conf > 0.0 and closed_conf >= open_conf
        yawning = yawn_conf > 0.0
        phone_detected = phone_conf > 0.0
        looking_forward = (
            face_detected
            and away_conf == 0.0
            and (forward_conf > 0.0 or not policy.labels(policy.forward_classes))
        )
        if face_detected and forward_conf == 0.0 and away_conf == 0.0:
            looking_forward = True
        if behavior is not None:
            looking_forward = behavior == "awake"

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
            behavior=behavior,
            behavior_confidence=behavior_confidence,
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
        if isinstance(imgsz, bool) or not isinstance(imgsz, int) or imgsz <= 0:
            raise ValueError("imgsz must be a positive integer")
        self.model_path = Path(model_path)
        self.model_version = model_version or self.model_path.name
        self.policy = policy or YoloDmsPolicy()
        self.estimator = YoloDmsEstimator(self.policy)
        self.device = device
        self.imgsz = imgsz
        self.half = half
        self.verbose = verbose
        self._model = model
        self.last_detections: tuple[YoloDetection, ...] = ()

    @property
    def model(self) -> Any:
        if self._model is None:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"DMS model does not exist: {self.model_path}")
            try:
                ultralytics = importlib.import_module("ultralytics")
            except ImportError as exc:
                raise RuntimeError("ultralytics is required for YOLO DMS inference") from exc
            self._model = ultralytics.YOLO(str(self.model_path), task="detect")
            self.validate_classes(self._model.names)
        return self._model

    def validate_classes(self, names: Any) -> None:
        values = names.values() if isinstance(names, dict) else names
        labels = {_normalize_label(str(value)) for value in values}
        behavioral = set().union(
            *(
                self.policy.labels(getattr(self.policy, name))
                for name in (
                    "eyes_closed_classes",
                    "eyes_open_classes",
                    "drowsy_classes",
                    "yawn_classes",
                    "forward_classes",
                    "away_classes",
                )
            )
        )
        if not labels.intersection(behavioral):
            raise ValueError(
                "Model has no configured DMS behavior classes; supply a trained DMS model and class mapping"
            )

    def detections(self, frame: Any) -> tuple[YoloDetection, ...]:
        model = self.model
        precision = {"half": self.half}
        if type(model).__module__.startswith("ultralytics."):
            cfg = importlib.import_module("ultralytics.cfg")
            if "quantize" in cfg.DEFAULT_CFG_DICT:
                precision = {"quantize": 16 if self.half else None}
        results = model.predict(
            frame, imgsz=self.imgsz, device=self.device, verbose=self.verbose, **precision
        )
        if not results:
            return ()
        self.validate_classes(results[0].names)
        ignored = self.policy.labels(self.policy.ignored_classes)
        return tuple(item for item in detections_from_ultralytics(results[0]) if item.label not in ignored)

    def state_from_frame(
        self,
        frame: Any,
        *,
        header: Header,
        camera_id: str,
    ) -> DriverState:
        started_ns = time.perf_counter_ns()
        self.last_detections = self.detections(frame)
        observation = self.estimator.observe(
            self.last_detections,
            capture_time_ns=header.capture_time_ns,
        )
        inference_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        return DriverState(
            header=replace(header, publish_time_ns=time.time_ns()),
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
            behavior=observation.behavior,
            behavior_confidence=observation.behavior_confidence,
        )
