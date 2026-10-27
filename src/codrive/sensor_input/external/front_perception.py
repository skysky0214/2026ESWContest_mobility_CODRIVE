"""YOLO front-camera perception publisher for objects, traffic lights, and lanes.

The module deliberately publishes compact :class:`ExternalPerception` records;
raw camera images and display overlays stay on the Jetson.  Its lane estimate is
image-space only until the camera calibration supplies a metric lane offset.
"""

from __future__ import annotations

import argparse
import importlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..driver.dms import YoloDetection, detections_from_ultralytics
from ...hardware.camera import GmslCameraConfig, open_opencv_camera
from ... import latency_probe as latency
from ...contracts.models import BoundingBox, ExternalPerception, Header, TrackedObject, TrafficLight, to_json
from .traffic_signal import TrafficSignalTracker
from ...transport.model_bus import JsonBus, publish_model
from ...transport import CycloneDdsBus, InMemoryBus


# COCO road users and road-side obstacles relevant to an advisory ADAS.
# A custom road-obstacle model can later add cones, construction barriers, and debris.
COCO_FRONT_CLASSES = (0, 1, 2, 3, 5, 7, 9, 11)
_CLASS_NAMES = {
    "person": "pedestrian",
    "bicycle": "bicycle",
    "car": "car",
    "motorcycle": "motorcycle",
    "bus": "bus",
    "truck": "truck",
    "traffic light": "traffic_light",
    "stop sign": "stop_sign",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _frame_dimensions(frame: Any) -> tuple[int, int]:
    shape = getattr(frame, "shape", ())
    if len(shape) < 2 or shape[0] <= 0 or shape[1] <= 0:
        raise ValueError("frame must have a positive HxW shape")
    return int(shape[0]), int(shape[1])


def normalized_bbox(bbox_xyxy: tuple[float, float, float, float], frame: Any) -> BoundingBox:
    """Convert a detector's pixel xyxy bounding box to the wire format."""

    height, width = _frame_dimensions(frame)
    x1, y1, x2, y2 = bbox_xyxy
    return BoundingBox(
        _clamp01(min(x1, x2) / width),
        _clamp01(min(y1, y2) / height),
        _clamp01(max(x1, x2) / width),
        _clamp01(max(y1, y2) / height),
    )


def traffic_light_from_bgr(frame: Any, bbox_xyxy: tuple[float, float, float, float]) -> tuple[TrafficLight, float]:
    """Classify the dominant illuminated colour inside a YOLO traffic-light ROI."""

    try:
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for traffic-light classification") from exc
    x1, y1, x2, y2 = (int(value) for value in bbox_xyxy)
    height, width = _frame_dimensions(frame)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    roi = frame[y1:y2, x1:x2]
    if getattr(roi, "size", 0) == 0:
        return TrafficLight.UNKNOWN, 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 80, 100), (10, 255, 255)) | cv2.inRange(hsv, (170, 80, 100), (180, 255, 255))
    masks = {
        TrafficLight.RED: red,
        TrafficLight.YELLOW: cv2.inRange(hsv, (18, 80, 100), (38, 255, 255)),
        TrafficLight.GREEN: cv2.inRange(hsv, (40, 60, 80), (90, 255, 255)),
    }
    state, pixels = max(masks.items(), key=lambda item: cv2.countNonZero(item[1]))
    pixel_count = cv2.countNonZero(pixels)
    roi_area = max(1, (x2 - x1) * (y2 - y1))
    if pixel_count < 10:
        return TrafficLight.UNKNOWN, 0.0
    return state, _clamp01(pixel_count / roi_area)


def lane_confidence_from_bgr(frame: Any) -> float:
    """Return an image-space lane confidence using HSV, Canny, ROI, and Hough lines."""

    try:
        cv2 = importlib.import_module("cv2")
        np = importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError("OpenCV and numpy are required for lane detection") from exc
    height, width = _frame_dimensions(frame)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    colour = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 0, 155), (180, 85, 255)),
        cv2.inRange(hsv, (14, 65, 80), (42, 255, 255)),
    )
    edges = cv2.Canny(cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0), 30, 100)
    edges = cv2.bitwise_and(edges, colour)
    mask = np.zeros_like(edges)
    cv2.fillPoly(mask, [np.array([(0, height), (width // 2 - 90, int(height * .55)), (width // 2 + 90, int(height * .55)), (width, height)])], 255)
    lines = cv2.HoughLinesP(cv2.bitwise_and(edges, mask), 1, np.pi / 180, 25, minLineLength=20, maxLineGap=130)
    if lines is None:
        return 0.0
    candidates = sum(1 for x1, y1, x2, y2 in (line.flatten() for line in lines) if abs(int(y2) - int(y1)) >= abs(int(x2) - int(x1)))
    return _clamp01(candidates / 8.0)


class UltralyticsFrontPerception:
    def __init__(self, model_path: str | Path, *, model: Any | None = None, imgsz: int = 640, device: str | int | None = None, verbose: bool = False, refinement: Any | None = None, estimate_image_lane: bool = True) -> None:
        self.estimate_image_lane = estimate_image_lane
        self.refinement = refinement
        self.signal_tracker = TrafficSignalTracker(min_detection_confidence=refinement.minimum if refinement else .60)
        self.model_path = Path(model_path)
        self.model_version = self.model_path.name
        if refinement:self.model_version += ":signal_refinement"
        self._model = model
        self.imgsz, self.device, self.verbose = imgsz, device, verbose

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                self._model = importlib.import_module("ultralytics").YOLO(str(self.model_path))
            except ImportError as exc:
                raise RuntimeError("ultralytics is required for front-camera inference") from exc
        return self._model

    def detections(self, frame: Any) -> tuple[YoloDetection, ...]:
        results = self.model.predict(frame, classes=list(COCO_FRONT_CLASSES), imgsz=self.imgsz, device=self.device, verbose=self.verbose)
        base = () if not results else detections_from_ultralytics(results[0])
        if self.refinement is None:return base
        from .signal_refinement import merge_signals
        extra = self.refinement.crop_detections(frame,self.model,self.imgsz,self.device,self.verbose)
        return self.refinement.filter(merge_signals(base,extra,self.refinement.minimum))

    def perception_from_frame(self, frame: Any, *, header: Header, camera_id: str) -> ExternalPerception:
        started_ns = time.perf_counter_ns()
        detections = self.detections(frame)
        self.last_detections = detections
        objects: list[TrackedObject] = []
        lights = []
        for index, detection in enumerate(detections):
            if detection.bbox_xyxy is None:
                continue
            label = _CLASS_NAMES.get(detection.label, detection.label)
            if label == 'traffic_light' and detection.confidence < self.signal_tracker.min_detection_confidence:
                continue
            bbox = normalized_bbox(detection.bbox_xyxy, frame)
            if label == "traffic_light":
                state, support = (self.refinement.state(frame,detection.bbox_xyxy) if self.refinement else traffic_light_from_bgr(frame, detection.bbox_xyxy))
                if self.refinement and (state == TrafficLight.UNKNOWN or support < self.refinement.config.get('minimum_state_evidence', .65)):
                    continue
                lights.append(((bbox.x1,bbox.y1,bbox.x2,bbox.y2),state,support,detection.confidence))
            objects.append(TrackedObject(index, label, detection.confidence, bbox))
        light, light_confidence = self.signal_tracker.update(lights)
        lane_confidence = lane_confidence_from_bgr(frame) if self.estimate_image_lane else 0.0
        return ExternalPerception(header=header, camera_id=camera_id, model_version=self.model_version, objects=tuple(objects), traffic_light=light, traffic_light_confidence=light_confidence, lane_confidence=lane_confidence, inference_ms=(time.perf_counter_ns() - started_ns) / 1_000_000)


@dataclass(slots=True)
class YoloFrontPublisher:
    bus: JsonBus
    perception: UltralyticsFrontPerception
    source_id: str = "jetson-front.front"
    camera_id: str = "front"
    frame_id: str = "camera_front_optical"
    sequence: int = 0
    geometry: Any = None

    def publish_frame(self, frame: Any, *, capture_time_ns: int | None = None, lane_boundaries=()) -> ExternalPerception:
        self.sequence += 1
        header = Header.now(self.source_id, self.sequence, frame_id=self.frame_id, capture_time_ns=capture_time_ns)
        result = self.perception.perception_from_frame(frame, header=header, camera_id=self.camera_id)
        if self.geometry is not None:
            result = self.geometry.enrich(result, lane_boundaries)
        if latency.enabled():latency.link(dds_publish_start_ns=time.time_ns())
        publish_model(self.bus, "external_perception", result)
        if latency.enabled():latency.link(dds_publish_end_ns=time.time_ns())
        return result


def run_camera_loop(*, bus: JsonBus, perception: UltralyticsFrontPerception, camera: GmslCameraConfig, max_frames: int | None = None, print_json: bool = False) -> int:
    publisher = YoloFrontPublisher(bus, perception)
    capture = open_opencv_camera(camera)
    published = 0
    try:
        while max_frames is None or published < max_frames:
            capture_time_ns = time.time_ns()
            ok, frame = capture.read()
            if not ok:
                continue
            output = publisher.publish_frame(frame, capture_time_ns=capture_time_ns)
            published += 1
            if print_json:
                print(to_json(output), flush=True)
    finally:
        capture.release()
    return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run front YOLO perception and publish ExternalPerception.")
    parser.add_argument("--model", default="models/yolo11n.engine")
    parser.add_argument("--domain-id", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--sensor-id", type=int, default=0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--device", default=None)
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args(argv)
    bus: JsonBus = InMemoryBus() if args.dry_run else CycloneDdsBus(domain_id=args.domain_id)
    try:
        return run_camera_loop(bus=bus, perception=UltralyticsFrontPerception(args.model, device=args.device, imgsz=args.imgsz), camera=GmslCameraConfig(sensor_id=args.sensor_id, width=args.width, height=args.height, fps=args.fps), max_frames=args.max_frames, print_json=args.print_json)
    except KeyboardInterrupt:
        return 130
    finally:
        close = getattr(bus, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
