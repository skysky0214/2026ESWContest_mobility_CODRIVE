"""Shared publication boundary for both statically assigned Jetson nodes."""

from __future__ import annotations

from typing import Any

from ..contracts.models import AudioEvent, DriverState, ExternalPerception
from ..contracts.envelope import DdsEnvelope
from ..transport.model_bus import JsonBus, publish_model


class JetsonPerceptionPublisher:
    """Prevent one Jetson role from publishing another node's source ID."""

    def __init__(self, bus: JsonBus, source_id: str) -> None:
        self.bus = bus
        self.source_id = source_id

    def _check_source(self, model: Any) -> None:
        model_source = model.header.source_id
        if model_source != self.source_id and not model_source.startswith(f"{self.source_id}."):
            raise ValueError(f"worker {self.source_id!r} cannot publish source {model_source!r}")

    def external(self, model: ExternalPerception) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "external_perception", model)

    def driver(self, model: DriverState) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "driver_state", model)

    def audio(self, model: AudioEvent) -> DdsEnvelope:
        self._check_source(model)
        return publish_model(self.bus, "audio_event", model)


__all__ = ["JetsonPerceptionPublisher"]
