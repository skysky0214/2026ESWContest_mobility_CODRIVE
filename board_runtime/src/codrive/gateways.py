"""Raspberry Pi sensor gateways with injectable vehicle-specific decoding."""

from __future__ import annotations

from typing import Protocol, Sequence

from .hardware.novatel_ins import NovatelInsParser
from .hardware.panda_can import CanFrame, PandaCanReceiver
from .models import Header, InsState, VehicleState
from .runtime import JsonBus, publish_model


class VehicleStateDecoder(Protocol):
    """Implemented only after validating the exact EV6 fingerprint and DBC."""

    def decode(self, frames: Sequence[CanFrame], header: Header) -> VehicleState | None: ...


class PandaGateway:
    def __init__(
        self,
        bus: JsonBus,
        receiver: PandaCanReceiver,
        decoder: VehicleStateDecoder,
        *,
        source_id: str = "pi5.panda",
    ) -> None:
        self.bus = bus
        self.receiver = receiver
        self.decoder = decoder
        self.source_id = source_id
        self.sequence = 0

    def poll(self, *, capture_time_ns: int | None = None) -> VehicleState | None:
        frames = self.receiver.read_available()
        if not frames:
            return None
        self.sequence += 1
        header = Header.now(
            self.source_id,
            self.sequence,
            frame_id="vehicle",
            capture_time_ns=capture_time_ns,
        )
        state = self.decoder.decode(frames, header)
        if state is None:
            return None
        if state.header != header:
            raise ValueError("vehicle decoder must preserve the gateway header")
        publish_model(self.bus, "vehicle_state", state)
        return state


class NovatelGateway:
    def __init__(
        self,
        bus: JsonBus,
        parser: NovatelInsParser,
        *,
        source_id: str = "pi5.ins",
    ) -> None:
        self.bus = bus
        self.parser = parser
        self.source_id = source_id
        self.sequence = 0

    def feed_line(
        self, line: str | bytes, *, capture_time_ns: int | None = None
    ) -> InsState | None:
        parsed = self.parser.feed_line(line, received_time_ns=capture_time_ns)
        if parsed is None:
            return None
        self.sequence += 1
        position = parsed.position
        heading = parsed.heading
        state = InsState(
            header=Header.now(
                self.source_id,
                self.sequence,
                frame_id="ins",
                capture_time_ns=capture_time_ns,
            ),
            latitude_deg=None if position is None else position.latitude_deg,
            longitude_deg=None if position is None else position.longitude_deg,
            height_m=None if position is None else position.height_m,
            east_m=parsed.east_m,
            north_m=parsed.north_m,
            yaw_rad=parsed.yaw_rad,
            pitch_deg=None if heading is None else heading.pitch_deg,
            fix_quality=parsed.fix_quality,
            satellites=parsed.satellites,
            solution_status="unknown" if position is None else position.solution_status,
        )
        publish_model(self.bus, "ins_state", state)
        return state
