from __future__ import annotations

import pytest
from cyclonedds.idl import IdlStruct
from cyclonedds.qos import Policy

from codrive.transport import (
    CRITICAL_EVENT,
    CONTEXT_STATE,
    DEFAULT_TOPIC_REGISTRY,
    EVENT_LATEST,
    HEALTH,
    HMI_LATEST,
    SENSOR_LATEST,
    STATE_LATEST,
    BusClosedError,
    CycloneDdsBus,
    DdsEnvelope,
    EnvelopeValidationError,
    InMemoryBus,
    MAX_PAYLOAD_UTF8_BYTES,
    QoSProfile,
    SchemaVersionError,
    TopicRegistry,
    TopicSpec,
    UnknownTopicError,
    canonicalize_payload_json,
    validate_payload_json,
)


def test_envelope_is_one_serializable_idl_type_with_canonical_json() -> None:
    assert issubclass(DdsEnvelope, IdlStruct)
    payload_json = canonicalize_payload_json('{ "한글": true, "b": 2, "a": 1 }')
    assert payload_json == '{"a":1,"b":2,"한글":true}'

    envelope = DdsEnvelope("jetson-left", 4, 10, 12, 1, payload_json)
    decoded = DdsEnvelope.deserialize(DdsEnvelope.serialize(envelope))

    assert decoded == envelope
    assert decoded.payload == {"a": 1, "b": 2, "한글": True}


@pytest.mark.parametrize(
    "payload",
    [
        '{"duplicate":1,"duplicate":2}',
        '{"not_finite":NaN}',
        "[]",
    ],
)
def test_canonicalizer_rejects_unsafe_payloads(payload: str) -> None:
    with pytest.raises(EnvelopeValidationError):
        canonicalize_payload_json(payload)


def test_canonical_validator_rejects_valid_but_noncanonical_json() -> None:
    with pytest.raises(EnvelopeValidationError, match="not canonical"):
        validate_payload_json('{"b": 2, "a": 1}')


def test_envelope_rejects_oversized_payload() -> None:
    with pytest.raises(EnvelopeValidationError, match="exceeds"):
        canonicalize_payload_json({"data": "x" * MAX_PAYLOAD_UTF8_BYTES})


def test_default_registry_has_only_declared_versioned_topics() -> None:
    expected = {
        "external_perception": "codrive/v1/perception/external",
        "driver_state": "codrive/v1/perception/driver",
        "audio_event": "codrive/v1/perception/audio",
        "environment_state": "codrive/v1/environment/state",
        "vehicle_state": "codrive/v1/vehicle/state",
        "turn_indicator_state": "codrive/v1/vehicle/turn_indicator",
        "scenario_context": "codrive/v1/demo/scenario_context",
        "ins_state": "codrive/v1/localization/ins",
        "situation_snapshot": "codrive/v1/fusion/snapshot",
        "risk_event": "codrive/v1/risk/event",
        "hmi_command": "codrive/v1/hmi/command",
        "node_health": "codrive/v1/system/health",
    }
    assert {key: spec.name for key, spec in DEFAULT_TOPIC_REGISTRY.items()} == expected
    assert (
        DEFAULT_TOPIC_REGISTRY.resolve(expected["vehicle_state"])
        == (DEFAULT_TOPIC_REGISTRY["vehicle_state"])
    )
    with pytest.raises(UnknownTopicError):
        DEFAULT_TOPIC_REGISTRY.resolve("codrive/v1/not-registered")


def test_topic_registry_rejects_duplicate_wire_names() -> None:
    duplicate = TopicSpec("test/same", QoSProfile.SENSOR_LATEST)
    with pytest.raises(ValueError, match="duplicate"):
        TopicRegistry({"one": duplicate, "two": duplicate})


def test_qos_profiles_express_transport_contract() -> None:
    assert SENSOR_LATEST[Policy.History] == Policy.History.KeepLast(1)
    assert SENSOR_LATEST[Policy.Reliability] == Policy.Reliability.BestEffort
    assert SENSOR_LATEST[Policy.Durability] == Policy.Durability.Volatile
    assert SENSOR_LATEST[Policy.Lifespan] is not None
    assert SENSOR_LATEST[Policy.Deadline] is not None

    assert STATE_LATEST[Policy.History] == Policy.History.KeepLast(1)
    assert isinstance(STATE_LATEST[Policy.Reliability], Policy.Reliability.Reliable)
    assert STATE_LATEST[Policy.Durability] == Policy.Durability.Volatile

    assert CRITICAL_EVENT[Policy.History] == Policy.History.KeepLast(16)
    assert isinstance(CRITICAL_EVENT[Policy.Reliability], Policy.Reliability.Reliable)
    assert CRITICAL_EVENT[Policy.Durability] == Policy.Durability.TransientLocal

    assert EVENT_LATEST[Policy.Deadline] is None
    assert EVENT_LATEST[Policy.History] == Policy.History.KeepLast(8)
    assert CONTEXT_STATE[Policy.Deadline] is None
    assert CONTEXT_STATE[Policy.Durability] == Policy.Durability.TransientLocal
    assert HMI_LATEST[Policy.History] == Policy.History.KeepLast(1)

    assert HEALTH[Policy.History] == Policy.History.KeepLast(1)
    assert isinstance(HEALTH[Policy.Reliability], Policy.Reliability.Reliable)
    assert HEALTH[Policy.Deadline] is not None


def test_in_memory_bus_applies_latest_only_and_supports_full_topic_name() -> None:
    bus = InMemoryBus(clock_ns=lambda: 100)
    bus.publish_json("external_perception", {"frame": 1}, source_id="jetson-1", sequence=1)
    second = bus.publish_json(
        "codrive/v1/perception/external",
        '{"frame": 2}',
        source_id="jetson-1",
        sequence=2,
        capture_time_ns=90,
    )

    samples = bus.take_json("external_perception", max_samples=10)

    assert [sample.sequence for sample in samples] == [2]
    assert samples[0].payload == {"frame": 2}
    assert second.capture_time_ns == 90
    assert second.publish_time_ns == 100


def test_latest_history_is_per_dds_source_instance() -> None:
    bus = InMemoryBus(clock_ns=lambda: 100)
    bus.publish_json("external_perception", {"frame": 1}, source_id="front", sequence=1)
    bus.publish_json("external_perception", {"frame": 2}, source_id="rear", sequence=1)
    bus.publish_json("external_perception", {"frame": 3}, source_id="front", sequence=2)

    samples = bus.take_json("external_perception", max_samples=10)

    assert [(sample.source_id, sample.payload["frame"]) for sample in samples] == [
        ("rear", 2),
        ("front", 3),
    ]


def test_in_memory_critical_events_retain_bounded_history() -> None:
    bus = InMemoryBus(clock_ns=lambda: 100)
    for sequence in range(70):
        bus.publish_json(
            "risk_event", {"event": sequence}, source_id="controller", sequence=sequence
        )

    samples = bus.take_json("risk_event", max_samples=100)

    assert len(samples) == 16
    assert samples[0].sequence == 54
    assert samples[-1].sequence == 69


def test_bus_rejects_unknown_topic_schema_mismatch_and_use_after_close() -> None:
    bus = InMemoryBus()
    with pytest.raises(UnknownTopicError):
        bus.publish_json("typo", {}, source_id="test", sequence=0)
    with pytest.raises(SchemaVersionError):
        bus.publish_json("node_health", {}, source_id="test", sequence=0, schema_version=2)

    bus.close()
    bus.close()
    with pytest.raises(BusClosedError):
        bus.take_json("node_health")


def test_context_manager_closes_in_memory_bus() -> None:
    with InMemoryBus() as bus:
        bus.publish_json("node_health", {"state": "ready"}, source_id="pi", sequence=1)
    assert bus.closed


def test_local_cyclone_participant_smoke_without_discovery_wait() -> None:
    bus = CycloneDdsBus(domain_id=213)
    assert bus.participant is not None
    bus.close()
    assert bus.closed


def test_peer_departure_notification_does_not_hide_valid_payload(monkeypatch) -> None:
    from cyclonedds.sub import InvalidSample
    from types import SimpleNamespace

    valid = DdsEnvelope("jetson-cabin", 1, 100, 101, 1, '{"state":"ready"}')
    with CycloneDdsBus(domain_id=212) as bus:
        monkeypatch.setattr(bus, "_reader_for", lambda spec: SimpleNamespace(
            take=lambda N: [InvalidSample(b"", None), valid],
        ))
        assert bus.take_json("node_health", 32) == [valid]
