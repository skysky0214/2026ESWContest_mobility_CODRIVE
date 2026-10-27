"""Defensive parsers for the NovAtel ASCII logs used by CODRIVE."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


WGS84_SEMI_MAJOR_M = 6_378_137.0
WGS84_FLATTENING = 1.0 / 298.257223563
MAX_LINE_LENGTH = 8_192
CRC32_POLYNOMIAL = 0xEDB88320


@dataclass(frozen=True, slots=True)
class EnuOrigin:
    latitude_deg: float
    longitude_deg: float
    height_m: float = 0.0

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError("origin latitude must be in [-90, 90]")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError("origin longitude must be in [-180, 180]")
        values = (self.latitude_deg, self.longitude_deg, self.height_m)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("origin values must be finite")


@dataclass(frozen=True, slots=True)
class BestPosition:
    solution_status: str
    position_type: str
    latitude_deg: float
    longitude_deg: float
    height_m: float
    undulation_m: float | None
    gps_week: int
    gps_seconds: float


@dataclass(frozen=True, slots=True)
class Heading2:
    solution_status: str
    position_type: str
    baseline_m: float
    heading_deg: float
    pitch_deg: float
    gps_week: int
    gps_seconds: float

    @property
    def enu_yaw_rad(self) -> float:
        return heading_to_enu_yaw(self.heading_deg)


@dataclass(frozen=True, slots=True)
class GgaStatus:
    utc_time: str
    latitude_deg: float | None
    longitude_deg: float | None
    fix_quality: int
    satellites: int
    hdop: float | None
    altitude_m: float | None
    geoid_separation_m: float | None

    @property
    def has_fix(self) -> bool:
        return self.fix_quality > 0


@dataclass(frozen=True, slots=True)
class InsSnapshot:
    """Latest independently received position, heading, and GNSS-status records."""

    position: BestPosition | None
    heading: Heading2 | None
    gga: GgaStatus | None
    east_m: float | None
    north_m: float | None
    up_m: float | None
    yaw_rad: float | None

    @property
    def latitude_deg(self) -> float | None:
        return None if self.position is None else self.position.latitude_deg

    @property
    def longitude_deg(self) -> float | None:
        return None if self.position is None else self.position.longitude_deg

    @property
    def fix_quality(self) -> int:
        return 0 if self.gga is None else self.gga.fix_quality

    @property
    def satellites(self) -> int:
        return 0 if self.gga is None else self.gga.satellites


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _ascii_line(line: str | bytes) -> str | None:
    if isinstance(line, bytes):
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            return None
    elif isinstance(line, str):
        text = line
    else:
        return None
    text = text.strip()
    if not text or len(text) > MAX_LINE_LENGTH or "\x00" in text:
        return None
    return text


def _novatel_fields(
    line: str | bytes,
    message_name: str,
) -> tuple[list[str], list[str]] | None:
    text = _ascii_line(line)
    if text is None or ";" not in text:
        return None
    if "*" in text and not _valid_novatel_crc(text):
        return None
    header_text, body_text = text.split(";", 1)
    header = [item.strip() for item in header_text.split(",")]
    if not header or header[0].upper() != f"#{message_name}":
        return None
    body_without_crc = body_text.split("*", 1)[0]
    body = [item.strip() for item in body_without_crc.split(",")]
    return header, body


def novatel_crc32(data: bytes) -> int:
    """Calculate the OEM7 reflected CRC-32 (initial value 0, no final XOR)."""

    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ CRC32_POLYNOMIAL if crc & 1 else crc >> 1
    return crc & 0xFFFFFFFF


def _valid_novatel_crc(text: str) -> bool:
    if not text.startswith("#") or "*" not in text:
        return False
    message, checksum_text = text.rsplit("*", 1)
    if len(checksum_text) != 8:
        return False
    try:
        expected = int(checksum_text, 16)
        payload = message[1:].encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return False
    return novatel_crc32(payload) == expected


def parse_bestposa(line: str | bytes) -> BestPosition | None:
    """Parse a NovAtel ``#BESTPOSA`` line, returning ``None`` when malformed."""

    fields = _novatel_fields(line, "BESTPOSA")
    if fields is None:
        return None
    header, body = fields
    try:
        if len(header) < 7 or len(body) < 7:
            return None
        gps_week = int(header[5])
        gps_seconds = _finite_float(header[6])
        latitude = _finite_float(body[2])
        longitude = _finite_float(body[3])
        height = _finite_float(body[4])
        undulation = _finite_float(body[5]) if body[5] else None
        if gps_week < 0 or gps_seconds < 0.0:
            return None
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            return None
        if not body[0] or not body[1]:
            return None
        return BestPosition(
            solution_status=body[0],
            position_type=body[1],
            latitude_deg=latitude,
            longitude_deg=longitude,
            height_m=height,
            undulation_m=undulation,
            gps_week=gps_week,
            gps_seconds=gps_seconds,
        )
    except (ValueError, TypeError, OverflowError):
        return None


def parse_heading2a(line: str | bytes) -> Heading2 | None:
    """Parse a NovAtel ``#HEADING2A`` line, returning ``None`` when malformed."""

    fields = _novatel_fields(line, "HEADING2A")
    if fields is None:
        return None
    header, body = fields
    try:
        if len(header) < 7 or len(body) < 5:
            return None
        gps_week = int(header[5])
        gps_seconds = _finite_float(header[6])
        baseline = _finite_float(body[2])
        heading = _finite_float(body[3])
        pitch = _finite_float(body[4])
        if gps_week < 0 or gps_seconds < 0.0 or baseline < 0.0:
            return None
        if not 0.0 <= heading <= 360.0 or not -90.0 <= pitch <= 90.0:
            return None
        if not body[0] or not body[1]:
            return None
        return Heading2(
            solution_status=body[0],
            position_type=body[1],
            baseline_m=baseline,
            heading_deg=heading % 360.0,
            pitch_deg=pitch,
            gps_week=gps_week,
            gps_seconds=gps_seconds,
        )
    except (ValueError, TypeError, OverflowError):
        return None


def _valid_nmea_checksum(text: str) -> tuple[str, bool]:
    if "*" not in text:
        return text, True
    payload, checksum_text = text.rsplit("*", 1)
    if len(checksum_text) != 2:
        return payload, False
    try:
        expected = int(checksum_text, 16)
    except ValueError:
        return payload, False
    calculated = 0
    for character in payload[1:] if payload.startswith("$") else payload:
        calculated ^= ord(character)
    return payload, calculated == expected


def _nmea_coordinate(value: str, hemisphere: str, degree_digits: int) -> float:
    if len(value) < degree_digits + 2:
        raise ValueError("short NMEA coordinate")
    degrees = int(value[:degree_digits])
    minutes = _finite_float(value[degree_digits:])
    if not 0.0 <= minutes < 60.0:
        raise ValueError("invalid NMEA coordinate minutes")
    coordinate = degrees + minutes / 60.0
    hemisphere = hemisphere.upper()
    if hemisphere in ("S", "W"):
        coordinate = -coordinate
    elif hemisphere not in ("N", "E"):
        raise ValueError("invalid NMEA hemisphere")
    return coordinate


def parse_gpgga(line: str | bytes) -> GgaStatus | None:
    """Parse a checksummed ``$GPGGA`` (or modern ``$GNGGA``) status sentence."""

    text = _ascii_line(line)
    if text is None:
        return None
    payload, checksum_ok = _valid_nmea_checksum(text)
    if not checksum_ok:
        return None
    fields = [item.strip() for item in payload.split(",")]
    if len(fields) < 14 or fields[0].upper() not in ("$GPGGA", "$GNGGA"):
        return None
    try:
        fix_quality = int(fields[6])
        satellites = int(fields[7])
        if not 0 <= fix_quality <= 9 or not 0 <= satellites <= 99:
            return None
        latitude = None
        longitude = None
        if fields[2] or fields[4]:
            if not all((fields[2], fields[3], fields[4], fields[5])):
                return None
            latitude = _nmea_coordinate(fields[2], fields[3], 2)
            longitude = _nmea_coordinate(fields[4], fields[5], 3)
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
                return None
        hdop = _finite_float(fields[8]) if fields[8] else None
        altitude = _finite_float(fields[9]) if fields[9] else None
        geoid = _finite_float(fields[11]) if fields[11] else None
        if hdop is not None and hdop < 0.0:
            return None
        if not fields[1]:
            return None
        return GgaStatus(
            utc_time=fields[1],
            latitude_deg=latitude,
            longitude_deg=longitude,
            fix_quality=fix_quality,
            satellites=satellites,
            hdop=hdop,
            altitude_m=altitude,
            geoid_separation_m=geoid,
        )
    except (ValueError, TypeError, OverflowError):
        return None


def heading_to_enu_yaw(heading_deg: float) -> float:
    """Convert clockwise-from-north heading to ENU CCW-from-east yaw in [-pi, pi]."""

    if not math.isfinite(heading_deg):
        raise ValueError("heading must be finite")
    yaw = math.radians(90.0 - heading_deg)
    return math.atan2(math.sin(yaw), math.cos(yaw))


def approximate_local_enu(
    latitude_deg: float,
    longitude_deg: float,
    height_m: float,
    origin: EnuOrigin,
) -> tuple[float, float, float]:
    """Short-baseline WGS-84 tangent-plane approximation around ``origin``."""

    values = (latitude_deg, longitude_deg, height_m)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("position values must be finite")
    if not -90.0 <= latitude_deg <= 90.0 or not -180.0 <= longitude_deg <= 180.0:
        raise ValueError("position is outside latitude/longitude bounds")

    reference_latitude = math.radians(origin.latitude_deg)
    latitude_delta = math.radians(latitude_deg - origin.latitude_deg)
    longitude_delta = math.radians(longitude_deg - origin.longitude_deg)
    longitude_delta = (longitude_delta + math.pi) % (2.0 * math.pi) - math.pi

    eccentricity_squared = WGS84_FLATTENING * (2.0 - WGS84_FLATTENING)
    sin_latitude = math.sin(reference_latitude)
    denominator = math.sqrt(1.0 - eccentricity_squared * sin_latitude * sin_latitude)
    prime_vertical_radius = WGS84_SEMI_MAJOR_M / denominator
    meridian_radius = (
        WGS84_SEMI_MAJOR_M
        * (1.0 - eccentricity_squared)
        / (denominator * denominator * denominator)
    )
    east = (
        longitude_delta * (prime_vertical_radius + origin.height_m) * math.cos(reference_latitude)
    )
    north = latitude_delta * (meridian_radius + origin.height_m)
    up = height_m - origin.height_m
    return east, north, up


class NovatelInsParser:
    """Statefully combine the latest valid BESTPOSA, HEADING2A, and GGA records."""

    __slots__ = (
        "origin",
        "component_ttl_ns",
        "_position",
        "_position_time_ns",
        "_heading",
        "_heading_time_ns",
        "_gga",
        "_gga_time_ns",
    )

    def __init__(self, origin: EnuOrigin | None = None, *, component_ttl_ms: int = 500) -> None:
        if not isinstance(component_ttl_ms, int) or component_ttl_ms <= 0:
            raise ValueError("component_ttl_ms must be a positive integer")
        self.origin = origin
        self.component_ttl_ns = component_ttl_ms * 1_000_000
        self._position: BestPosition | None = None
        self._position_time_ns = 0
        self._heading: Heading2 | None = None
        self._heading_time_ns = 0
        self._gga: GgaStatus | None = None
        self._gga_time_ns = 0

    def feed_line(
        self, line: str | bytes, *, received_time_ns: int | None = None
    ) -> InsSnapshot | None:
        """Consume one line; malformed or unrelated lines do not change state."""

        now_ns = time.time_ns() if received_time_ns is None else received_time_ns
        if not isinstance(now_ns, int) or now_ns < 0:
            raise ValueError("received_time_ns must be a non-negative integer")
        text = _ascii_line(line)
        if text is None:
            return None
        if text.upper().startswith("#BESTPOSA,"):
            parsed_position = parse_bestposa(text)
            if parsed_position is None:
                return None
            self._position = parsed_position
            self._position_time_ns = now_ns
        elif text.upper().startswith("#HEADING2A,"):
            parsed_heading = parse_heading2a(text)
            if parsed_heading is None:
                return None
            self._heading = parsed_heading
            self._heading_time_ns = now_ns
        elif text.upper().startswith(("$GPGGA,", "$GNGGA,")):
            parsed_gga = parse_gpgga(text)
            if parsed_gga is None:
                return None
            self._gga = parsed_gga
            self._gga_time_ns = now_ns
        else:
            return None
        return self.snapshot(now_ns=now_ns)

    def snapshot(self, *, now_ns: int | None = None) -> InsSnapshot:
        now_ns = time.time_ns() if now_ns is None else now_ns

        def fresh(value: object | None, timestamp_ns: int) -> bool:
            return (
                value is not None
                and timestamp_ns <= now_ns
                and now_ns - timestamp_ns <= self.component_ttl_ns
            )

        position = self._position if fresh(self._position, self._position_time_ns) else None
        heading = self._heading if fresh(self._heading, self._heading_time_ns) else None
        gga = self._gga if fresh(self._gga, self._gga_time_ns) else None
        east = north = up = None
        if position is not None and self.origin is not None:
            east, north, up = approximate_local_enu(
                position.latitude_deg,
                position.longitude_deg,
                position.height_m,
                self.origin,
            )
        pose_time_coherent = (
            position is None
            or heading is None
            or (
                position.gps_week == heading.gps_week
                and abs(position.gps_seconds - heading.gps_seconds) <= 0.5
            )
        )
        yaw = None if heading is None or not pose_time_coherent else heading.enu_yaw_rad
        return InsSnapshot(
            position=position,
            heading=heading,
            gga=gga,
            east_m=east,
            north_m=north,
            up_m=up,
            yaw_rad=yaw,
        )


__all__ = [
    "BestPosition",
    "EnuOrigin",
    "GgaStatus",
    "Heading2",
    "InsSnapshot",
    "NovatelInsParser",
    "approximate_local_enu",
    "heading_to_enu_yaw",
    "novatel_crc32",
    "parse_bestposa",
    "parse_gpgga",
    "parse_heading2a",
]
