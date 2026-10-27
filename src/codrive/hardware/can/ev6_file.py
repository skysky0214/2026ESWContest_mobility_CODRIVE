"""Read-only EV6 CAN-log decoding for offline injection.

The decoder is derived from the ``hw`` branch's receive-only EV6 extraction
logic.  It neither opens Panda nor exposes a CAN transmit API.  The input is
one JSON object per line: ``{"ts": ..., "address": ..., "bytes": "...", "bus": ...}``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .panda import CanFrame
from ...contracts.models import Header, VehicleState

KPH_TO_MPS = 1_000.0 / 3_600.0
DEFAULT_STEER_RATIO = 14.25
DEFAULT_WHEELBASE_M = 2.900

ADDR_WHEEL_SPEEDS = 160
ADDR_ACCELERATOR = 53
ADDR_ACCELERATOR_BRAKE_ALT = 256
ADDR_ACCELERATOR_ALT = 261
ADDR_MDPS = 234
ADDR_STEERING_SENSORS = 293
ADDR_TCS = 373


@dataclass(slots=True)
class _Ev6State:
    wheel_speeds_kph: list[float] = field(default_factory=lambda: [0.0] * 4)
    moving_backward: bool = False
    steering_sensor_deg: float = 0.0
    mdps_steering_deg: float = 0.0
    accelerator_pct: float = 0.0
    brake_pressed: bool = False
    seen_wheel_speeds: bool = False
    seen_steering: bool = False
    signal_times_ns: dict[str, int] = field(default_factory=dict)


def _u16_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _s16_le(data: bytes, offset: int) -> int:
    value = _u16_le(data, offset)
    return value - 0x1_0000 if value & 0x8000 else value


def _bit_is_set(data: bytes, bit: int) -> bool:
    byte_index, bit_index = divmod(bit, 8)
    return byte_index < len(data) and bool(data[byte_index] & (1 << bit_index))


def iter_can_jsonl(path: str | Path) -> Iterator[tuple[int, CanFrame]]:
    """Yield validated ``(capture_time_ns, CanFrame)`` records from a JSONL file."""

    with Path(path).open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line)
                timestamp_ns = int(float(item["ts"]) * 1_000_000_000)
                frame = CanFrame(
                    arbitration_id=int(item["address"]),
                    data=bytes.fromhex(str(item["bytes"])),
                    bus=int(item["bus"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid CAN JSONL record at line {line_number}") from exc
            if timestamp_ns < 0:
                raise ValueError(f"negative timestamp at line {line_number}")
            yield timestamp_ns, frame


def grouped_can_jsonl(path: str | Path) -> Iterator[tuple[int, tuple[CanFrame, ...]]]:
    """Group consecutive records with the same log timestamp into one CAN batch."""

    timestamp_ns: int | None = None
    frames: list[CanFrame] = []
    for record_timestamp_ns, frame in iter_can_jsonl(path):
        if timestamp_ns is not None and record_timestamp_ns != timestamp_ns:
            yield timestamp_ns, tuple(frames)
            frames = []
        timestamp_ns = record_timestamp_ns
        frames.append(frame)
    if timestamp_ns is not None:
        yield timestamp_ns, tuple(frames)


class Ev6CanFileDecoder:
    """Stateful decoder for EV6 log frames, compatible with ``PandaGateway``'s decoder protocol."""

    def __init__(
        self,
        *,
        bus: int = 0,
        steer_ratio: float = DEFAULT_STEER_RATIO,
        wheelbase_m: float = DEFAULT_WHEELBASE_M,
        angle_source: str = "sensor",
        invert_steer: bool = False,
        steer_bias_deg: float = 0.0,
    ) -> None:
        if bus < 0 or steer_ratio <= 0.0 or wheelbase_m <= 0.0:
            raise ValueError("bus, steer_ratio, and wheelbase_m must be positive")
        if angle_source not in {"sensor", "mdps"}:
            raise ValueError("angle_source must be 'sensor' or 'mdps'")
        self.bus = bus
        self.steer_ratio = steer_ratio
        self.wheelbase_m = wheelbase_m
        self.angle_source = angle_source
        self.invert_steer = invert_steer
        self.steer_bias_deg = steer_bias_deg
        self._state = _Ev6State()

    def decode(self, frames: Sequence[CanFrame], header: Header) -> VehicleState:
        for frame in frames:
            if frame.bus == self.bus:
                self._update(frame, header.capture_time_ns)
        state = self._state
        speed_mps = sum(state.wheel_speeds_kph) / 4.0 * KPH_TO_MPS
        steering_deg = state.mdps_steering_deg if self.angle_source == "mdps" else state.steering_sensor_deg
        if self.invert_steer:
            steering_deg *= -1.0
        steering_deg -= self.steer_bias_deg
        yaw_rate_dps = math.degrees(
            speed_mps / self.wheelbase_m * math.tan(math.radians(steering_deg) / self.steer_ratio)
        )
        return VehicleState(
            header=header,
            speed_mps=speed_mps,
            steering_wheel_deg=steering_deg,
            brake_pressed=state.brake_pressed,
            accelerator_pct=state.accelerator_pct,
            gear="reverse" if state.moving_backward else "drive",
            yaw_rate_dps=yaw_rate_dps,
            can_valid=state.seen_wheel_speeds or state.seen_steering,
            signal_times_ns=tuple(sorted(state.signal_times_ns.items())),
        )

    def _update(self, frame: CanFrame, capture_time_ns: int) -> None:
        address, data, state = frame.arbitration_id, frame.data, self._state
        if address == ADDR_WHEEL_SPEEDS and len(data) >= 16:
            state.wheel_speeds_kph = [_u16_le(data, offset) * 0.03125 for offset in (8, 10, 12, 14)]
            state.moving_backward = bool(data[7] & 0x0A)
            state.seen_wheel_speeds = True
            state.signal_times_ns["speed_mps"] = capture_time_ns
        elif address == ADDR_STEERING_SENSORS and len(data) >= 6:
            state.steering_sensor_deg = _s16_le(data, 3) * 0.1
            if self.angle_source == "sensor":
                state.signal_times_ns["steering_wheel_deg"] = capture_time_ns
            state.seen_steering = True
        elif address == ADDR_MDPS and len(data) >= 18:
            state.mdps_steering_deg = _s16_le(data, 16) * 0.1
            if self.angle_source == "mdps":
                state.signal_times_ns["steering_wheel_deg"] = capture_time_ns
            state.seen_steering = True
        elif address == ADDR_ACCELERATOR and len(data) >= 6:
            state.accelerator_pct = data[5] / 255.0 * 100.0
            state.signal_times_ns["accelerator_pct"] = capture_time_ns
        elif address == ADDR_ACCELERATOR_ALT and len(data) >= 15:
            raw = ((data[12] >> 7) | (data[13] << 1) | ((data[14] & 1) << 9)) & 0x3FF
            state.accelerator_pct = min(raw / 1022.0 * 100.0, 100.0)
            state.signal_times_ns["accelerator_pct"] = capture_time_ns
        elif address == ADDR_ACCELERATOR_BRAKE_ALT and len(data) >= 23:
            state.brake_pressed = _bit_is_set(data, 32)
            state.signal_times_ns["brake_pressed"] = capture_time_ns
        elif address == ADDR_TCS and len(data) >= 11:
            brake_light = (data[9] >> 2) & 0x3
            state.brake_pressed = bool(brake_light or ((data[10] >> 6) & 1) or ((data[10] >> 4) & 1))
            state.signal_times_ns["brake_pressed"] = capture_time_ns
