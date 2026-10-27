"""Hardware boundary adapters; optional vendor libraries are imported lazily."""

from .camera import (
    CameraConfig,
    GmslCameraConfig,
    UsbCameraConfig,
    build_gmsl_gstreamer,
    build_usb_webcam_gstreamer,
    camera_source,
    open_opencv_camera,
)
from .novatel_ins import (
    BestPosition,
    EnuOrigin,
    GgaStatus,
    Heading2,
    InsSnapshot,
    NovatelInsParser,
    approximate_local_enu,
    heading_to_enu_yaw,
    novatel_crc32,
    parse_bestposa,
    parse_gpgga,
    parse_heading2a,
)
from .panda_can import (
    CanFrame,
    PandaCanReceiver,
    PandaReadError,
    PandaReceiveProtocol,
    PandaUnavailableError,
)
from .stm32_audio import (
    AudioFrame,
    AudioStreamDecoder,
    encode_audio_frame,
)

__all__ = [
    "AudioFrame",
    "AudioStreamDecoder",
    "BestPosition",
    "CameraConfig",
    "CanFrame",
    "EnuOrigin",
    "GgaStatus",
    "GmslCameraConfig",
    "Heading2",
    "InsSnapshot",
    "NovatelInsParser",
    "PandaCanReceiver",
    "PandaReadError",
    "PandaReceiveProtocol",
    "PandaUnavailableError",
    "UsbCameraConfig",
    "approximate_local_enu",
    "build_gmsl_gstreamer",
    "build_usb_webcam_gstreamer",
    "camera_source",
    "encode_audio_frame",
    "heading_to_enu_yaw",
    "novatel_crc32",
    "open_opencv_camera",
    "parse_bestposa",
    "parse_gpgga",
    "parse_heading2a",
]
