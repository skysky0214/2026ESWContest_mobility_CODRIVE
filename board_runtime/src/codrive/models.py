"""Hardware-independent domain models shared by all CODRIVE nodes.

The timestamps named ``*_time_ns`` are synchronized Unix/PTP timestamps and may
be compared across hosts. ``monotonic_ns`` is only useful inside the source host
for measuring local latency. Raw images and audio are deliberately absent: edge
workers publish compact perception results over DDS.
"""

from __future__ import annotations

import json
import math
import time
import types
import uuid
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum, IntEnum
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints


SCHEMA_VERSION = 1
PROCESS_BOOT_ID = uuid.uuid4().hex


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    CRITICAL = 3


class TrafficLight(str, Enum):
    UNKNOWN = "unknown"
    OFF = "off"
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class TurnSignal(str, Enum):
    OFF = "off"
    LEFT = "left"
    RIGHT = "right"
    HAZARD = "hazard"


class AudioKind(str, Enum):
    NONE = "none"
    SIREN = "siren"
    HORN = "horn"
    OTHER = "other"


class Weather(str, Enum):
    UNKNOWN = "unknown"
    CLEAR = "clear"
    RAIN = "rain"
    SNOW = "snow"
    FOG = "fog"
    CLOUDY = "cloudy"
    THUNDER = "thunder"


class NodeState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


def _number(
    name: str,
    value: float | int | None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")


def _nonempty(name: str, value: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


@dataclass(frozen=True, slots=True)
class Header:
    source_id: str
    sequence: int
    capture_time_ns: int
    publish_time_ns: int
    monotonic_ns: int
    frame_id: str = ""
    boot_id: str = ""
    trace_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _nonempty("source_id", self.source_id, maximum=128)
        for name, value in (
            ("sequence", self.sequence),
            ("capture_time_ns", self.capture_time_ns),
            ("publish_time_ns", self.publish_time_ns),
            ("monotonic_ns", self.monotonic_ns),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.publish_time_ns < self.capture_time_ns:
            raise ValueError("publish_time_ns must not precede capture_time_ns")
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")

    @classmethod
    def now(
        cls,
        source_id: str,
        sequence: int,
        *,
        frame_id: str = "",
        capture_time_ns: int | None = None,
        boot_id: str | None = None,
        trace_id: str = "",
    ) -> "Header":
        wall = time.time_ns()
        return cls(
            source_id=source_id,
            sequence=sequence,
            capture_time_ns=wall if capture_time_ns is None else capture_time_ns,
            publish_time_ns=wall,
            monotonic_ns=time.monotonic_ns(),
            frame_id=frame_id,
            boot_id=boot_id or PROCESS_BOOT_ID,
            trace_id=trace_id or uuid.uuid4().hex,
        )


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Normalized xyxy image coordinates in [0, 1]."""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.x1 <= self.x2 <= 1.0 and 0.0 <= self.y1 <= self.y2 <= 1.0):
            raise ValueError("bounding box must be normalized xyxy coordinates")


@dataclass(frozen=True, slots=True)
class TrackedObject:
    track_id: int
    class_name: str
    confidence: float
    bbox: BoundingBox
    distance_m: float | None = None
    relative_speed_mps: float | None = None
    ttc_s: float | None = None
    zone: str = "front"
    in_path: bool | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.track_id, bool)
            or not isinstance(self.track_id, int)
            or self.track_id < 0
        ):
            raise ValueError("track_id must be a non-negative integer")
        _nonempty("class_name", self.class_name)
        _number("confidence", self.confidence, minimum=0.0, maximum=1.0)
        _number("distance_m", self.distance_m, minimum=0.0, optional=True)
        _number("relative_speed_mps", self.relative_speed_mps, optional=True)
        _number("ttc_s", self.ttc_s, minimum=0.0, optional=True)
        _nonempty("zone", self.zone)


@dataclass(frozen=True, slots=True)
class ExternalPerception:
    header: Header
    camera_id: str
    model_version: str
    objects: tuple[TrackedObject, ...] = ()
    traffic_light: TrafficLight = TrafficLight.UNKNOWN
    traffic_light_confidence: float = 0.0
    lane_offset_m: float | None = None
    lane_heading_error_deg: float | None = None
    lane_confidence: float = 0.0
    inference_ms: float = 0.0
    degraded_reason: str = ""

    def __post_init__(self) -> None:
        _nonempty("camera_id", self.camera_id)
        _nonempty("model_version", self.model_version)
        _number(
            "traffic_light_confidence",
            self.traffic_light_confidence,
            minimum=0.0,
            maximum=1.0,
        )
        _number("lane_offset_m", self.lane_offset_m, optional=True)
        _number("lane_heading_error_deg", self.lane_heading_error_deg, optional=True)
        _number("lane_confidence", self.lane_confidence, minimum=0.0, maximum=1.0)
        _number("inference_ms", self.inference_ms, minimum=0.0)


@dataclass(frozen=True, slots=True)
class DriverState:
    header: Header
    camera_id: str
    model_version: str
    attention_score: float
    looking_forward: bool
    eyes_closed: bool = False
    phone_detected: bool = False
    face_detected: bool = True
    inference_ms: float = 0.0
    drowsiness_score: float = 0.0
    microsleep_ms: int = 0
    yawning: bool = False
    behavior: str | None = None
    behavior_confidence: float | None = None

    def __post_init__(self) -> None:
        _nonempty("camera_id", self.camera_id)
        _nonempty("model_version", self.model_version)
        _number("attention_score", self.attention_score, minimum=0.0, maximum=1.0)
        _number("inference_ms", self.inference_ms, minimum=0.0)
        if self.behavior not in {None, "awake", "distracted", "drowsy", "head_drop", "phone"}:
            raise ValueError("Unknown driver behavior")
        _number("behavior_confidence", self.behavior_confidence, optional=True, minimum=0.0, maximum=1.0)
        _number("drowsiness_score", self.drowsiness_score, minimum=0.0, maximum=1.0)
        if isinstance(self.microsleep_ms, bool) or not isinstance(self.microsleep_ms, int) or self.microsleep_ms < 0:
            raise ValueError("microsleep_ms must be a non-negative integer")
        for name in ("looking_forward", "eyes_closed", "phone_detected", "face_detected", "yawning"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")


@dataclass(frozen=True, slots=True)
class AudioEvent:
    header: Header
    kind: AudioKind
    confidence: float
    window_ms: int
    model_version: str
    direction: str = "unknown"

    def __post_init__(self) -> None:
        _number("confidence", self.confidence, minimum=0.0, maximum=1.0)
        if (
            isinstance(self.window_ms, bool)
            or not isinstance(self.window_ms, int)
            or self.window_ms <= 0
        ):
            raise ValueError("window_ms must be a positive integer")
        _nonempty("model_version", self.model_version)
        _nonempty("direction", self.direction)


@dataclass(frozen=True, slots=True)
class VehicleState:
    header: Header
    speed_mps: float
    steering_wheel_deg: float
    brake_pressed: bool
    accelerator_pct: float
    turn_signal: TurnSignal = TurnSignal.OFF
    hazard_lights: bool = False
    headlights_on: bool = False
    wiper_level: int = 0
    gear: str = "unknown"
    yaw_rate_dps: float | None = None
    can_valid: bool = True
    # Actual reception times per decoded CAN signal; missing entries are unverified.
    signal_times_ns: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _number("speed_mps", self.speed_mps, minimum=0.0, maximum=120.0)
        _number("steering_wheel_deg", self.steering_wheel_deg, minimum=-2_000, maximum=2_000)
        _number("accelerator_pct", self.accelerator_pct, minimum=0.0, maximum=100.0)
        if isinstance(self.wiper_level, bool) or not isinstance(self.wiper_level, int):
            raise TypeError("wiper_level must be an integer")
        if not 0 <= self.wiper_level <= 10:
            raise ValueError("wiper_level must be in [0, 10]")
        _number("yaw_rate_dps", self.yaw_rate_dps, optional=True)
        _nonempty("gear", self.gear)
        names = set()
        for name, timestamp in self.signal_times_ns:
            if name not in {"speed_mps", "accelerator_pct", "brake_pressed", "steering_wheel_deg"}:
                raise ValueError("unknown CAN signal timestamp")
            if name in names or type(timestamp) is not int or not 0 <= timestamp <= self.header.capture_time_ns:
                raise ValueError("invalid or duplicate CAN signal timestamp")
            names.add(name)


@dataclass(frozen=True, slots=True)
class InsState:
    header: Header
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    height_m: float | None = None
    east_m: float | None = None
    north_m: float | None = None
    yaw_rad: float | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    yaw_rate_dps: float | None = None
    longitudinal_accel_mps2: float | None = None
    fix_quality: int = 0
    satellites: int = 0
    solution_status: str = "unknown"

    def __post_init__(self) -> None:
        _number("latitude_deg", self.latitude_deg, minimum=-90.0, maximum=90.0, optional=True)
        _number("longitude_deg", self.longitude_deg, minimum=-180.0, maximum=180.0, optional=True)
        for name in (
            "height_m",
            "east_m",
            "north_m",
            "yaw_rad",
            "roll_deg",
            "pitch_deg",
            "yaw_rate_dps",
            "longitudinal_accel_mps2",
        ):
            _number(name, getattr(self, name), optional=True)
        if not isinstance(self.fix_quality, int) or not 0 <= self.fix_quality <= 9:
            raise ValueError("fix_quality must be an integer in [0, 9]")
        if not isinstance(self.satellites, int) or not 0 <= self.satellites <= 128:
            raise ValueError("satellites must be an integer in [0, 128]")


@dataclass(frozen=True, slots=True)
class EnvironmentState:
    header: Header
    weather: Weather = Weather.UNKNOWN
    is_night: bool = False
    precipitation_mm_h: float = 0.0
    visibility_m: float | None = None
    temperature_c: float | None = None
    api_valid: bool = False
    snowfall_cm_h: float = 0.0
    weather_time_ns: int | None = None
    weather_valid_until_ns: int | None = None
    daylight_valid: bool | None = None
    sunrise_time_ns: int | None = None
    sunset_time_ns: int | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None

    def __post_init__(self) -> None:
        _number("precipitation_mm_h", self.precipitation_mm_h, minimum=0.0)
        _number("visibility_m", self.visibility_m, minimum=0.0, optional=True)
        _number("temperature_c", self.temperature_c, optional=True)
        _number("snowfall_cm_h", self.snowfall_cm_h, minimum=0.0)
        _number("latitude_deg", self.latitude_deg, minimum=-90, maximum=90, optional=True)
        _number("longitude_deg", self.longitude_deg, minimum=-180, maximum=180, optional=True)
        for name in ("weather_time_ns", "weather_valid_until_ns", "sunrise_time_ns", "sunset_time_ns"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative timestamp")
        if self.daylight_valid is not None and type(self.daylight_valid) is not bool:
            raise TypeError("daylight_valid must be a boolean or null")


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """Explicit, expiring demo preset; never contains object or sound detections."""
    header: Header
    scenario_id: str
    environment: EnvironmentState | None = None
    audio_alerts: bool = False

    def __post_init__(self):
        if type(self.audio_alerts) is not bool:
            raise TypeError("audio_alerts must be boolean")
        if self.scenario_id not in ("rain-night-slow-vehicle", "emergency-yellow", "pedestrian-lane", "rapid-acceleration", ""):
            raise ValueError("Unknown scenario preset")
        if self.environment is not None and not self.scenario_id:
            raise ValueError("Environment override requires an explicit demo")
        if self.audio_alerts and self.scenario_id != "emergency-yellow":
            raise ValueError("Audio override is restricted to the emergency demo")
        if self.environment is not None:
            rain=self.scenario_id=="rain-night-slow-vehicle"
            if (self.environment.header != self.header or self.environment.is_night != rain
                    or self.environment.weather is not (Weather.RAIN if rain else Weather.CLEAR)):
                raise ValueError("Scenario must carry matching explicit weather/daylight context")


@dataclass(frozen=True, slots=True)
class TurnIndicatorState:
    """Actual G29 switch intent; separate source from CAN ECU measurements."""
    header: Header
    turn_signal: TurnSignal
    connected: bool

    def __post_init__(self):
        if not isinstance(self.turn_signal, TurnSignal):
            raise TypeError("turn_signal must be a TurnSignal")
        if type(self.connected) is not bool:
            raise TypeError("connected must be boolean")


@dataclass(frozen=True, slots=True)
class SituationSnapshot:
    header: Header
    vehicle: VehicleState | None = None
    ins: InsState | None = None
    external: tuple[ExternalPerception, ...] = ()
    driver: DriverState | None = None
    audio: AudioEvent | None = None
    environment: EnvironmentState | None = None
    stale_sources: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskEvent:
    header: Header
    event_id: str
    code: str
    severity: Severity
    score: float
    recommended_action: str
    message_key: str
    evidence: tuple[str, ...]
    expires_at_ns: int

    def __post_init__(self) -> None:
        _nonempty("event_id", self.event_id)
        _nonempty("code", self.code)
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be a Severity")
        _number("score", self.score, minimum=0.0)
        _nonempty("recommended_action", self.recommended_action)
        _nonempty("message_key", self.message_key)
        if self.expires_at_ns < self.header.capture_time_ns:
            raise ValueError("risk event expires before capture")


@dataclass(frozen=True, slots=True)
class HmiCommand:
    header: Header
    event_id: str
    risk_code: str
    severity: Severity
    display_text: str
    tts_text: str
    icon: str
    display_ms: int
    source: str = "template"
    expires_at_ns: int = 0

    def __post_init__(self) -> None:
        _nonempty("event_id", self.event_id)
        _nonempty("risk_code", self.risk_code)
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be a Severity")
        _nonempty("display_text", self.display_text)
        _nonempty("tts_text", self.tts_text)
        _nonempty("icon", self.icon)
        if not isinstance(self.display_ms, int) or not 100 <= self.display_ms <= 30_000:
            raise ValueError("display_ms must be in [100, 30000]")
        _nonempty("source", self.source)
        if self.expires_at_ns < self.header.capture_time_ns:
            raise ValueError("HMI command expires before capture")


@dataclass(frozen=True, slots=True)
class NodeHealth:
    header: Header
    node_id: str
    state: NodeState
    uptime_s: float
    queue_depth: int = 0
    dropped_samples: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        _nonempty("node_id", self.node_id)
        _number("uptime_s", self.uptime_s, minimum=0.0)
        for name, value in (
            ("queue_depth", self.queue_depth),
            ("dropped_samples", self.dropped_samples),
        ):
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


ModelT = TypeVar("ModelT")


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    return value


def to_json(value: Any) -> str:
    """Serialize a model as deterministic, compact JSON."""

    return json.dumps(
        _primitive(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _convert(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        candidates = [item for item in args if item is not type(None)]
        return _convert(candidates[0], value) if candidates else None
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise TypeError("expected array")
        if args and args[-1] is not Ellipsis:
            if len(value) != len(args):
                raise ValueError("tuple length mismatch")
            return tuple(_convert(kind, item) for kind, item in zip(args, value))
        inner = args[0] if args else Any
        return tuple(_convert(inner, item) for item in value)
    if origin is list:
        if not isinstance(value, list):
            raise TypeError("expected array")
        inner = args[0] if args else Any
        return [_convert(inner, item) for item in value]
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, dict):
            raise TypeError("expected object")
        return from_dict(annotation, value)
    if annotation is bool:
        if not isinstance(value, bool):
            raise TypeError("expected boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("expected integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("expected number")
        if not math.isfinite(value):
            raise ValueError("expected finite number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise TypeError("expected string")
        return value
    return value


def from_dict(model_type: type[ModelT], payload: dict[str, Any]) -> ModelT:
    """Rebuild a nested dataclass model from a decoded JSON object."""

    hints = get_type_hints(model_type)
    known = {field.name for field in fields(model_type)}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"unknown {model_type.__name__} fields: {sorted(unknown)}")
    values = {name: _convert(hints.get(name, Any), value) for name, value in payload.items()}
    return model_type(**values)


def from_json(model_type: type[ModelT], payload_json: str) -> ModelT:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("model JSON root must be an object")
    return from_dict(model_type, payload)
