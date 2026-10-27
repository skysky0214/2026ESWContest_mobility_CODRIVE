"""The single IDL wire type used by every CODRIVE DDS topic.

Domain models intentionally stay outside the DDS type system.  They are encoded
as deterministic JSON and carried by :class:`DdsEnvelope`; the DDS topic name is
the discriminator that tells a receiver which domain schema to apply.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from cyclonedds.idl import IdlStruct
from cyclonedds.idl.annotations import key as idl_key
from cyclonedds.idl.types import uint32, uint64

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
MAX_PAYLOAD_UTF8_BYTES = 60_000


class EnvelopeValidationError(ValueError):
    """Raised when envelope metadata or its JSON payload is not wire-safe."""


def _reject_json_constant(token: str) -> None:
    raise EnvelopeValidationError(f"non-finite JSON number is not allowed: {token}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EnvelopeValidationError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _load_json_object(payload_json: str) -> dict[str, JsonValue]:
    if not isinstance(payload_json, str):
        raise TypeError("payload_json must be a string")
    try:
        value = json.loads(
            payload_json,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except EnvelopeValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnvelopeValidationError("payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EnvelopeValidationError("payload must be a JSON object")
    return value


def _canonical_dump(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EnvelopeValidationError("payload contains a non-JSON value") from exc
    # Parsing the result also verifies string keys and guards future encoder changes.
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_UTF8_BYTES:
        raise EnvelopeValidationError(f"payload exceeds {MAX_PAYLOAD_UTF8_BYTES} UTF-8 bytes")
    _load_json_object(encoded)
    return encoded


def canonicalize_payload_json(payload: str | Mapping[str, Any]) -> str:
    """Return the project's deterministic JSON representation of an object.

    String input may be ordinary valid JSON; it is parsed with duplicate-key and
    non-finite-number checks before being normalized.  Mapping input is encoded
    directly.  Only JSON objects are accepted because one topic represents one
    versioned domain-object schema.
    """

    if isinstance(payload, str):
        return _canonical_dump(_load_json_object(payload))
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a JSON object mapping or JSON string")
    return _canonical_dump(payload)


def validate_payload_json(payload_json: str) -> dict[str, JsonValue]:
    """Validate canonical payload JSON and return its decoded object.

    Receiving code uses this stricter function so a non-conforming publisher is
    rejected instead of introducing multiple byte representations of one value.
    """

    value = _load_json_object(payload_json)
    if _canonical_dump(value) != payload_json:
        raise EnvelopeValidationError("payload_json is valid but not canonical")
    return value


def _validate_uint(name: str, value: int, upper_bound: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeValidationError(f"{name} must be an integer")
    if not 0 <= value <= upper_bound:
        raise EnvelopeValidationError(f"{name} must be in [0, {upper_bound}]")


@dataclass
class DdsEnvelope(IdlStruct):
    """Common DDS sample shared by all CODRIVE topics."""

    source_id: str
    sequence: uint64
    capture_time_ns: uint64
    publish_time_ns: uint64
    schema_version: uint32
    payload_json: str

    # History depth is maintained per source instance. This is essential for the
    # shared external-perception and health topics with multiple publishers.
    idl_key("source_id")

    def __post_init__(self) -> None:
        validate_envelope(self)

    @property
    def payload(self) -> dict[str, JsonValue]:
        """Decode and validate the canonical JSON object."""

        return validate_payload_json(self.payload_json)


def validate_envelope(envelope: DdsEnvelope) -> DdsEnvelope:
    """Validate envelope metadata and canonical JSON, returning ``envelope``."""

    if not isinstance(envelope.source_id, str) or not envelope.source_id.strip():
        raise EnvelopeValidationError("source_id must be a non-empty string")
    _validate_uint("sequence", envelope.sequence, UINT64_MAX)
    _validate_uint("capture_time_ns", envelope.capture_time_ns, UINT64_MAX)
    _validate_uint("publish_time_ns", envelope.publish_time_ns, UINT64_MAX)
    if envelope.publish_time_ns < envelope.capture_time_ns:
        raise EnvelopeValidationError("publish_time_ns must not precede capture_time_ns")
    _validate_uint("schema_version", envelope.schema_version, UINT32_MAX)
    if envelope.schema_version == 0:
        raise EnvelopeValidationError("schema_version must be greater than zero")
    validate_payload_json(envelope.payload_json)
    return envelope


def make_envelope(
    payload: str | Mapping[str, Any],
    *,
    source_id: str,
    sequence: int,
    capture_time_ns: int,
    publish_time_ns: int,
    schema_version: int = 1,
) -> DdsEnvelope:
    """Build a validated envelope while canonicalizing its payload."""

    return DdsEnvelope(
        source_id=source_id,
        sequence=sequence,
        capture_time_ns=capture_time_ns,
        publish_time_ns=publish_time_ns,
        schema_version=schema_version,
        payload_json=canonicalize_payload_json(payload),
    )


__all__ = [
    "MAX_PAYLOAD_UTF8_BYTES",
    "DdsEnvelope",
    "EnvelopeValidationError",
    "JsonValue",
    "canonicalize_payload_json",
    "make_envelope",
    "validate_envelope",
    "validate_payload_json",
]
