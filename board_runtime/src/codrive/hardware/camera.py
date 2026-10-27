"""Validated camera-source builders for Jetson GMSL and USB cameras."""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Union


_DEVICE_PATTERN = re.compile(r"/dev/video[0-9]+\Z")
_PIXEL_FORMAT_PATTERN = re.compile(r"[A-Z0-9_]+\Z")


def _validate_dimensions(width: int, height: int, fps: int) -> None:
    for name, value, upper_bound in (
        ("width", width, 8192),
        ("height", height, 8192),
        ("fps", fps, 240),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper_bound:
            raise ValueError(f"{name} must be an integer in [1, {upper_bound}]")


def _validate_device(device: str) -> None:
    if not isinstance(device, str) or _DEVICE_PATTERN.fullmatch(device) is None:
        raise ValueError("device must look like /dev/videoN")


@dataclass(frozen=True, slots=True)
class GmslCameraConfig:
    backend: Literal["argus", "v4l2"] = "argus"
    sensor_id: int = 0
    device: str = "/dev/video0"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pixel_format: str = "UYVY"
    flip_method: int = 0

    def __post_init__(self) -> None:
        if self.backend not in ("argus", "v4l2"):
            raise ValueError("backend must be 'argus' or 'v4l2'")
        if (
            isinstance(self.sensor_id, bool)
            or not isinstance(self.sensor_id, int)
            or not 0 <= self.sensor_id <= 31
        ):
            raise ValueError("sensor_id must be an integer in [0, 31]")
        _validate_device(self.device)
        _validate_dimensions(self.width, self.height, self.fps)
        if _PIXEL_FORMAT_PATTERN.fullmatch(self.pixel_format) is None:
            raise ValueError("pixel_format contains unsupported characters")
        if (
            isinstance(self.flip_method, bool)
            or not isinstance(self.flip_method, int)
            or not 0 <= self.flip_method <= 7
        ):
            raise ValueError("flip_method must be an integer in [0, 7]")


@dataclass(frozen=True, slots=True)
class UsbCameraConfig:
    device_index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    input_format: Literal["MJPG", "YUY2"] = "MJPG"
    use_gstreamer: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.device_index, bool)
            or not isinstance(self.device_index, int)
            or not 0 <= self.device_index <= 255
        ):
            raise ValueError("device_index must be an integer in [0, 255]")
        _validate_dimensions(self.width, self.height, self.fps)
        if self.input_format not in ("MJPG", "YUY2"):
            raise ValueError("input_format must be MJPG or YUY2")


CameraConfig = Union[GmslCameraConfig, UsbCameraConfig]


def build_gmsl_gstreamer(config: GmslCameraConfig) -> str:
    """Create an OpenCV-compatible Jetson Argus or V4L2 GStreamer pipeline."""

    if not isinstance(config, GmslCameraConfig):
        raise TypeError("config must be GmslCameraConfig")
    if config.backend == "argus":
        source = (
            f"nvarguscamerasrc sensor-id={config.sensor_id} ! "
            f"video/x-raw(memory:NVMM),width=(int){config.width},height=(int){config.height},"
            f"format=(string)NV12,framerate=(fraction){config.fps}/1 ! "
            f"nvvidconv flip-method={config.flip_method} ! "
        )
    else:
        source = (
            f"v4l2src device={config.device} io-mode=2 ! "
            f"video/x-raw,width=(int){config.width},height=(int){config.height},"
            f"format=(string){config.pixel_format},framerate=(fraction){config.fps}/1 ! "
            f"nvvidconv flip-method={config.flip_method} ! "
        )
    return (
        source + "video/x-raw,format=(string)BGRx ! videoconvert ! "
        "video/x-raw,format=(string)BGR ! appsink drop=true max-buffers=1 sync=false"
    )


def build_usb_webcam_gstreamer(config: UsbCameraConfig) -> str:
    """Create a low-latency Linux USB-webcam GStreamer source."""

    if not isinstance(config, UsbCameraConfig):
        raise TypeError("config must be UsbCameraConfig")
    device = f"/dev/video{config.device_index}"
    if config.input_format == "MJPG":
        caps_and_decoder = (
            f"image/jpeg,width=(int){config.width},height=(int){config.height},"
            f"framerate=(fraction){config.fps}/1 ! jpegdec ! "
        )
    else:
        caps_and_decoder = (
            f"video/x-raw,format=(string)YUY2,width=(int){config.width},"
            f"height=(int){config.height},framerate=(fraction){config.fps}/1 ! "
        )
    return (
        f"v4l2src device={device} ! "
        + caps_and_decoder
        + "videoconvert ! video/x-raw,format=(string)BGR ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


def camera_source(config: CameraConfig) -> str | int:
    """Return a pipeline string, or a direct OpenCV index when explicitly requested."""

    if isinstance(config, GmslCameraConfig):
        return build_gmsl_gstreamer(config)
    if isinstance(config, UsbCameraConfig):
        if config.use_gstreamer:
            return build_usb_webcam_gstreamer(config)
        return config.device_index
    raise TypeError("unsupported camera configuration")


def open_opencv_camera(config: CameraConfig) -> Any:
    """Open a configured camera while keeping the optional OpenCV import lazy."""

    try:
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:
        raise RuntimeError("OpenCV is required to open a camera") from exc
    source = camera_source(config)
    if isinstance(source, str):
        capture = cv2.VideoCapture(source, cv2.CAP_GSTREAMER)
    else:
        capture = cv2.VideoCapture(source)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, config.height)
        capture.set(cv2.CAP_PROP_FPS, config.fps)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"failed to open camera source for {config!r}")
    return capture


__all__ = [
    "CameraConfig",
    "GmslCameraConfig",
    "UsbCameraConfig",
    "build_gmsl_gstreamer",
    "build_usb_webcam_gstreamer",
    "camera_source",
    "open_opencv_camera",
]
