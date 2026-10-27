"""Cabin DMS application: camera/replay, JSON, local risk and optional DDS.

Original estimator and preview: bake1004, source commit 4071f96.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from .. import latency_probe as latency
from ..config import load_config
from ..fusion import SituationFusion
from ..hardware.camera import UsbCameraConfig, open_opencv_camera
from ..models import DriverState, Header, NodeHealth, NodeState, to_json
from ..risk import RiskEngine
from ..runtime import JsonBus, publish_model
from ..sensor_input.driver.dms import UltralyticsYoloDms, YoloDmsPolicy
from .local_bus import LocalBus


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
        state = self.dms.state_from_frame(
            frame,
            header=Header.now(
                self.source_id,
                self.sequence,
                frame_id=self.frame_id,
                capture_time_ns=capture_time_ns,
            ),
            camera_id=self.camera_id,
        )
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
    rate_hz: float = 15,
    max_frames: int | None = None,
    stop: Event | None = None,
    print_json: bool = False,
    video: str | None = None,
    preview: bool = False,
    mirror: bool = False,
    local_risk: bool = False,
    max_read_failures: int = 5,
    node_id: str = "jetson-cabin",
    capture: Any = None,
    frame_sink: Callable[[Any], object] | None = None,
) -> int:
    if not math.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("rate_hz must be finite and positive")
    if max_frames is not None and (isinstance(max_frames, bool) or max_frames < 1):
        raise ValueError("max_frames must be positive")
    if max_read_failures < 1:
        raise ValueError("max_read_failures must be positive")
    cv2 = importlib.import_module("cv2") if capture is None or preview or mirror else None
    if capture is None:
        if video:
            if not Path(video).is_file():
                raise FileNotFoundError(f"Video does not exist: {video}")
            capture = cv2.VideoCapture(video)
            if not capture.isOpened():
                capture.release()
                raise RuntimeError(f"Cannot open video: {video}")
        else:
            capture = open_opencv_camera(camera)
    stop = stop or Event()
    publisher = YoloDmsPublisher(bus, dms, source_id, camera_id, frame_id)
    pipeline = SituationFusion() if local_risk else None
    risk_engine = RiskEngine() if local_risk else None
    frames = failures = health_sequence = 0
    started = time.monotonic()
    next_health = started
    next_tick = started
    replay_origin = time.time_ns()
    state_kind = NodeState.STARTING
    detail = "replay" if video else "camera"

    def health(kind: NodeState, message: str) -> None:
        nonlocal health_sequence
        health_sequence += 1
        publish_model(
            bus,
            "node_health",
            NodeHealth(
                header=Header.now(node_id, health_sequence),
                node_id=node_id,
                state=kind,
                uptime_s=time.monotonic() - started,
                dropped_samples=failures,
                detail=message,
            ),
        )

    try:
        fps = float(capture.get(5)) if video else rate_hz  # CAP_PROP_FPS
        if video and (not math.isfinite(fps) or fps <= 0):
            raise ValueError("Replay requires valid video FPS")
        health(NodeState.STARTING, detail)
        while not stop.is_set() and (max_frames is None or frames < max_frames):
            ok, frame = capture.read()
            if not ok:
                if video:
                    break
                failures += 1
                dms.estimator.reset()
                health(NodeState.DEGRADED, "camera_read_failed")
                if failures >= max_read_failures:
                    raise RuntimeError("Camera read failed repeatedly")
                stop.wait(1 / rate_hz)
                continue
            failures = 0
            captured = (
                replay_origin + round(frames * 1_000_000_000 / fps) if video else time.time_ns()
            )
            if mirror:
                frame = cv2.flip(frame, 1)
            trace=latency.begin('driver_frame')
            state = publisher.publish_frame(frame, capture_time_ns=captured)
            if trace is not None:
                latency.link(frame=latency.header_key(state.header),receipt_ns=captured)
                latency.finish(trace)
            if frame_sink is not None:
                try:
                    # HMI always receives clean video; boxes belong to the optional Jetson preview.
                    frame_sink(frame)
                except Exception:
                    # Video preview failure must not stop driver-state publishing.
                    logging.getLogger(__name__).exception("Cabin HMI preview disabled after frame error")
                    frame_sink = None
            frames += 1
            state_kind = NodeState.READY if state.face_detected else NodeState.DEGRADED
            detail = "replay" if video else "camera"
            if not state.face_detected:
                detail += ":face_missing"
            if print_json:
                print(to_json(state), flush=True)
            if pipeline:
                pipeline.ingest(state, received_time_ns=captured)
                snapshot = pipeline.snapshot(now_ns=captured)
                risk = risk_engine.evaluate(snapshot, now_ns=captured)
                print(to_json(risk or snapshot), flush=True)
            if time.monotonic() >= next_health:
                health(state_kind, detail)
                next_health = time.monotonic() + 0.5
            if preview:
                from .dms_preview import draw_state_overlay
                cv2.imshow(
                    "CODRIVE DMS",
                    draw_state_overlay(cv2, frame, dms, state),
                )
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
            next_tick = max(next_tick + 1 / (fps if video else rate_hz), time.monotonic())
            stop.wait(max(0, next_tick - time.monotonic()))
        if video and frames == 0:
            raise RuntimeError("Video yielded no decodable frames")
        return frames
    except Exception:
        health(NodeState.DEGRADED, "dms_execution_failed")
        raise
    finally:
        capture.release()
        if preview:
            cv2.destroyAllWindows()
        health(NodeState.DEGRADED, "dms_stopped")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/board/jetson-cabin.yaml")
    parser.add_argument("--model")
    parser.add_argument("--video", help="Replay local video using its frame time axis")
    parser.add_argument(
        "--dry-run", action="store_true", help="In-memory transport; still runs inference"
    )
    parser.add_argument("--local-risk", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--domain-id", type=int)
    parser.add_argument("--device-index", type=int)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=int)
    parser.add_argument("--input-format", choices=("MJPG", "YUY2"))
    parser.add_argument("--no-gstreamer", action="store_true")
    parser.add_argument("--imgsz", type=int)
    parser.add_argument("--device")
    parser.add_argument("--no-half", action="store_true")
    parser.add_argument("--hmi-stream-port", type=int)
    parser.add_argument("--hmi-stream-host")
    parser.add_argument("--no-hmi-stream", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    latency.install_cabin()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.video and not args.dry_run:
        parser.error("video replay requires --dry-run; do not mix replay with live DDS")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    cabin = next(item for item in config["cameras"] if item["camera_id"] == "cabin")
    source = cabin["source"]
    inference = next(item for item in cabin["pipelines"] if item["type"] == "driver_monitoring")
    model = Path(args.model or inference["engine"])
    if not model.is_absolute():
        # Checked-in profiles live under config/board; CLI paths are cwd-relative.
        model = (Path.cwd() if args.model else config_path.parents[2]) / model
    mapping = dict(inference.get("policy", {}))
    for name, value in mapping.items():
        if name.endswith("_classes"):
            mapping[name] = tuple(value)
    dms = UltralyticsYoloDms(
        model,
        device=args.device or inference.get("device", "0"),
        imgsz=args.imgsz if args.imgsz is not None else inference.get("imgsz", 640),
        half=not args.no_half and inference.get("half", True),
        policy=YoloDmsPolicy(**mapping),
    )
    _ = dms.model  # Validate local model before opening hardware or DDS.
    camera = UsbCameraConfig(
        device_index=args.device_index
        if args.device_index is not None
        else int(source["device"].removeprefix("/dev/video")),
        width=args.width if args.width is not None else source["width"],
        height=args.height if args.height is not None else source["height"],
        fps=args.fps if args.fps is not None else source["fps"],
        input_format=args.input_format or source.get("input_format", "MJPG"),
        use_gstreamer=not args.no_gstreamer,
    )
    if args.dry_run:
        bus = LocalBus()
    else:
        if "CYCLONEDDS_URI" not in os.environ:
            uri = config["dds"].get("config_uri")
            if uri:
                dds_path = Path(uri)
                if not dds_path.is_absolute():
                    dds_path = config_path.parents[2] / dds_path
                if not dds_path.is_file():
                    raise FileNotFoundError(f"DDS config does not exist: {dds_path}")
                os.environ["CYCLONEDDS_URI"] = dds_path.resolve().as_uri()
        from ..transport import CycloneDdsBus

        bus = CycloneDdsBus(
            domain_id=args.domain_id if args.domain_id is not None else config["dds"]["domain_id"]
        )
    stream = None
    try:
        stream_config = config.get("hmi_stream", {})
        if not args.no_hmi_stream and (stream_config.get("enabled") or args.hmi_stream_port is not None):
            from ..transport.mjpeg import MjpegStreamConfig, MjpegStreamServer

            stream = MjpegStreamServer(MjpegStreamConfig(
                host=args.hmi_stream_host or stream_config.get("host", "0.0.0.0"),
                port=args.hmi_stream_port if args.hmi_stream_port is not None else stream_config.get("port", 8082),
                width=stream_config.get("width", 640),
                fps=stream_config.get("fps", 10),
                jpeg_quality=stream_config.get("jpeg_quality", 70),
            ))
            try:
                stream.start()
            except (OSError, RuntimeError):
                logging.getLogger(__name__).exception("Cabin HMI preview unavailable; DMS continues")
                stream.close()
                stream = None
        run_camera_loop(
            bus=bus,
            dms=dms,
            camera=camera,
            video=args.video,
            max_frames=args.max_frames,
            print_json=args.print_json,
            preview=args.preview,
            mirror=args.mirror,
            local_risk=args.local_risk,
            source_id=cabin["source_id"],
            camera_id=cabin["camera_id"],
            frame_id=cabin["frame_id"],
            node_id=config["node_id"],
            rate_hz=config["publish"]["driver_state_hz"],
            frame_sink=stream.submit_bgr if stream else None,
        )
    except KeyboardInterrupt:
        return 130
    finally:
        if stream:
            stream.close()
        bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
