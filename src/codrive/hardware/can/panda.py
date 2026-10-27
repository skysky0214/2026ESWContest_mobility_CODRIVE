"""Receive-only adapter for comma.ai Panda CAN interfaces.

The vendor package is intentionally imported only by :meth:`PandaCanReceiver.connect`.
This module has no CAN transmit method, and a device opened by ``connect`` is put in
Panda's silent safety mode before it is returned to the caller.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from typing_extensions import Self

MAX_CAN_ID = 0x1FFFFFFF
VALID_PAYLOAD_LENGTHS = frozenset((*range(9), 12, 16, 20, 24, 32, 48, 64))


class PandaUnavailableError(RuntimeError):
    """Raised when the optional Panda package or a Panda device is unavailable."""


class PandaReadError(RuntimeError):
    """Raised when receiving from a connected Panda fails."""


@runtime_checkable
class PandaReceiveProtocol(Protocol):
    """Small receive-only surface used by :class:`PandaCanReceiver`.

    A real Panda and a test double both satisfy this protocol. Current Panda
    returns ``(address, data, bus)``; older versions may also include a hardware
    timestamp as the second of four fields.
    """

    def can_recv(self) -> Iterable[object]:
        """Return all currently available CAN packets."""

    def close(self) -> None:
        """Release the hardware handle."""


@dataclass(frozen=True, slots=True)
class CanFrame:
    """Validated classic-CAN or CAN-FD frame received from Panda."""

    arbitration_id: int
    data: bytes
    bus: int
    hardware_timestamp_us: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.arbitration_id, int) or not 0 <= self.arbitration_id <= MAX_CAN_ID:
            raise ValueError("arbitration_id must be a valid 11-bit or 29-bit CAN identifier")
        if not isinstance(self.bus, int) or self.bus < 0:
            raise ValueError("bus must be a non-negative integer")
        if self.hardware_timestamp_us is not None and (
            isinstance(self.hardware_timestamp_us, bool)
            or not isinstance(self.hardware_timestamp_us, int)
            or self.hardware_timestamp_us < 0
        ):
            raise ValueError("hardware_timestamp_us must be a non-negative integer")
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        if len(self.data) not in VALID_PAYLOAD_LENGTHS:
            raise ValueError("data length is not a valid CAN/CAN-FD payload length")

    @property
    def is_extended(self) -> bool:
        return self.arbitration_id > 0x7FF

    @property
    def is_fd(self) -> bool:
        return len(self.data) > 8


def _packet_field(packet: object, names: Sequence[str]) -> object:
    for name in names:
        if hasattr(packet, name):
            return getattr(packet, name)
    raise AttributeError(names[0])


def _decode_packet(packet: object) -> CanFrame:
    """Normalize Panda's tuple packets and newer attribute-based packet objects."""

    if isinstance(packet, (tuple, list)):
        if len(packet) == 3:
            address, payload, bus = packet
            timestamp = None
        elif len(packet) == 4:
            address, timestamp, payload, bus = packet
        else:
            raise ValueError("Panda packet tuple must have three or four fields")
    else:
        address = _packet_field(packet, ("address", "arbitration_id"))
        timestamp = _packet_field(packet, ("recv_time", "timestamp", "timestamp_us"))
        payload = _packet_field(packet, ("dat", "data"))
        bus = _packet_field(packet, ("src", "bus"))

    if isinstance(address, bool) or not isinstance(address, int):
        raise TypeError("CAN address must be an integer")
    if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, int)):
        raise TypeError("CAN timestamp must be an integer")
    if isinstance(bus, bool) or not isinstance(bus, int):
        raise TypeError("CAN bus must be an integer")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("CAN payload must be bytes-like")

    return CanFrame(
        arbitration_id=address,
        data=bytes(payload),
        bus=bus,
        hardware_timestamp_us=timestamp,
    )


class PandaCanReceiver:
    """Bounded, receive-only view of a Panda device.

    Invalid packets and packets outside ``allowed_buses`` are dropped.  Panda bus
    values carrying transmit-return flags are consequently excluded by the default
    ``{0, 1, 2, 3}`` bus allow-list.
    """

    __slots__ = ("__device", "_allowed_buses", "_closed", "_dropped_frames")

    def __init__(
        self,
        device: PandaReceiveProtocol,
        *,
        allowed_buses: Iterable[int] = (0, 1, 2, 3),
    ) -> None:
        buses = frozenset(allowed_buses)
        invalid_bus = any(
            isinstance(bus, bool) or not isinstance(bus, int) or bus < 0 for bus in buses
        )
        if not buses or invalid_bus:
            raise ValueError("allowed_buses must contain non-negative integer bus numbers")
        if not callable(getattr(device, "can_recv", None)):
            raise TypeError("device must provide can_recv()")
        self.__device = device
        self._allowed_buses = buses
        self._closed = False
        self._dropped_frames = 0

    @classmethod
    def connect(
        cls,
        serial: str | None = None,
        *,
        allowed_buses: Iterable[int] = (0, 1, 2, 3),
    ) -> PandaCanReceiver:
        """Open the official Panda package lazily and force silent safety mode."""

        try:
            panda_module = importlib.import_module("panda")
            panda_type = panda_module.Panda
            try:
                structs_module = importlib.import_module("opendbc.car.structs")
                silent_mode = structs_module.CarParams.SafetyModel.silent
            except (ImportError, AttributeError):
                # Compatibility with older standalone Panda releases.
                silent_mode = panda_type.SAFETY_SILENT
        except (ImportError, AttributeError) as exc:
            raise PandaUnavailableError(
                "comma.ai Panda and an explicit silent safety model are required"
            ) from exc

        device = None
        try:
            device = panda_type(serial) if serial is not None else panda_type()
            set_safety_mode = device.set_safety_mode
            set_safety_mode(silent_mode)
            return cls(device, allowed_buses=allowed_buses)
        except Exception as exc:
            if device is not None:
                close = getattr(device, "close", None)
                if callable(close):
                    close()
            raise PandaUnavailableError("failed to open Panda in silent receive-only mode") from exc

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def closed(self) -> bool:
        return self._closed

    def read_available(self, *, max_frames: int = 4096) -> tuple[CanFrame, ...]:
        """Read one bounded batch, dropping malformed packets without crashing."""

        if self._closed:
            raise PandaReadError("Panda receiver is closed")
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
            raise ValueError("max_frames must be a positive integer")

        try:
            packets = self.__device.can_recv()
        except Exception as exc:
            raise PandaReadError("Panda CAN receive failed") from exc

        if packets is None:
            return ()

        frames: list[CanFrame] = []
        try:
            for index, packet in enumerate(packets):
                if index >= max_frames:
                    self._dropped_frames += 1
                    break
                try:
                    frame = _decode_packet(packet)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    self._dropped_frames += 1
                    continue
                if frame.bus not in self._allowed_buses:
                    self._dropped_frames += 1
                    continue
                frames.append(frame)
        except Exception as exc:
            raise PandaReadError("Panda returned an invalid packet stream") from exc
        return tuple(frames)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.__device, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> Self:
        if self._closed:
            raise PandaReadError("Panda receiver is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = [
    "CanFrame",
    "PandaCanReceiver",
    "PandaReadError",
    "PandaReceiveProtocol",
    "PandaUnavailableError",
]
