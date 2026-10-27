"""Typed domain-model serialization on top of a JSON-envelope bus."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from ..contracts.envelope import DdsEnvelope
from ..contracts.models import from_json, to_json


class JsonBus(Protocol):
    def publish_json(
        self,
        topic: str,
        payload: str,
        *,
        source_id: str,
        sequence: int,
        capture_time_ns: int | None = None,
        publish_time_ns: int | None = None,
        schema_version: int | None = None,
    ) -> DdsEnvelope: ...

    def take_json(self, topic: str, max_samples: int = 1) -> list[DdsEnvelope]: ...


class ModelEnvelopeError(ValueError):
    """Raised when payload metadata differs from its DDS envelope."""


Model = TypeVar("Model")


def publish_model(bus: JsonBus, topic: str, model: Any) -> DdsEnvelope:
    header = getattr(model, "header", None)
    if header is None:
        raise TypeError("published domain models must contain a header")
    return bus.publish_json(
        topic,
        to_json(model),
        source_id=header.source_id,
        sequence=header.sequence,
        capture_time_ns=header.capture_time_ns,
        publish_time_ns=header.publish_time_ns,
        schema_version=header.schema_version,
    )


def decode_model(envelope: DdsEnvelope, model_type: type[Model]) -> Model:
    model = from_json(model_type, envelope.payload_json)
    header = getattr(model, "header", None)
    if header is None:
        raise ModelEnvelopeError("decoded model has no header")
    comparisons = {
        "source_id": (header.source_id, envelope.source_id),
        "sequence": (header.sequence, envelope.sequence),
        "capture_time_ns": (header.capture_time_ns, envelope.capture_time_ns),
        "publish_time_ns": (header.publish_time_ns, envelope.publish_time_ns),
        "schema_version": (header.schema_version, envelope.schema_version),
    }
    mismatched = [name for name, (inner, outer) in comparisons.items() if inner != outer]
    if mismatched:
        raise ModelEnvelopeError(f"payload/envelope mismatch: {', '.join(mismatched)}")
    return model


__all__ = ["JsonBus", "ModelEnvelopeError", "decode_model", "publish_model"]
