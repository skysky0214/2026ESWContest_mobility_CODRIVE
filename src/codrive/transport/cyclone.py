"""Cyclone DDS and deterministic in-memory transports with one shared API."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any

from cyclonedds.domain import DomainParticipant
from cyclonedds.pub import DataWriter
from cyclonedds.qos import Policy
from cyclonedds.sub import DataReader
from cyclonedds.topic import Topic

from ..contracts.envelope import DdsEnvelope, make_envelope, validate_envelope
from ..contracts.topics import (
    AUDIO_EVENT_TOPIC,
    DEFAULT_TOPIC_REGISTRY,
    DRIVER_STATE_TOPIC,
    ENVIRONMENT_STATE_TOPIC,
    EXTERNAL_PERCEPTION_TOPIC,
    HMI_COMMAND_TOPIC,
    INS_STATE_TOPIC,
    NODE_HEALTH_TOPIC,
    RISK_EVENT_TOPIC,
    SITUATION_SNAPSHOT_TOPIC,
    TOPIC_REGISTRY,
    TOPICS,
    VEHICLE_STATE_TOPIC,
    TopicRegistry,
    TopicSpec,
    UnknownTopicError,
)
from .qos import qos_for


class BusClosedError(RuntimeError):
    """Raised when an operation is attempted on a closed bus."""


class SchemaVersionError(ValueError):
    """Raised when an envelope does not match its registered topic schema."""


def _coerce_registry(
    registry: TopicRegistry | Mapping[str, TopicSpec],
) -> TopicRegistry:
    return registry if isinstance(registry, TopicRegistry) else TopicRegistry(registry)


def _validate_max_samples(max_samples: int) -> None:
    if isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples < 1:
        raise ValueError("max_samples must be a positive integer")


class _JsonBusBase:
    """Shared validation and context-manager behavior for transport backends."""

    def __init__(
        self,
        *,
        registry: TopicRegistry | Mapping[str, TopicSpec],
        clock_ns: Callable[[], int],
    ) -> None:
        self._registry = _coerce_registry(registry)
        self._clock_ns = clock_ns
        self._closed = False

    @property
    def registry(self) -> TopicRegistry:
        return self._registry

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise BusClosedError("DDS bus is closed")

    def _prepare_envelope(
        self,
        topic: str | TopicSpec,
        payload: str | Mapping[str, Any],
        *,
        source_id: str,
        sequence: int,
        capture_time_ns: int | None,
        publish_time_ns: int | None,
        schema_version: int | None,
    ) -> tuple[TopicSpec, DdsEnvelope]:
        self._ensure_open()
        spec = self._registry.resolve(topic)
        resolved_schema = spec.schema_version if schema_version is None else schema_version
        if resolved_schema != spec.schema_version:
            raise SchemaVersionError(
                f"topic {spec.name!r} requires schema version {spec.schema_version}, "
                f"got {resolved_schema}"
            )
        now = self._clock_ns() if publish_time_ns is None else publish_time_ns
        captured = now if capture_time_ns is None else capture_time_ns
        return spec, make_envelope(
            payload,
            source_id=source_id,
            sequence=sequence,
            capture_time_ns=captured,
            publish_time_ns=now,
            schema_version=resolved_schema,
        )

    @staticmethod
    def _validate_received(spec: TopicSpec, envelope: DdsEnvelope) -> DdsEnvelope:
        validate_envelope(envelope)
        if envelope.schema_version != spec.schema_version:
            raise SchemaVersionError(
                f"topic {spec.name!r} requires schema version {spec.schema_version}, "
                f"got {envelope.schema_version}"
            )
        return envelope

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


class CycloneDdsBus(_JsonBusBase):
    """Non-blocking JSON-envelope bus backed by Cyclone DDS.

    A topic, writer, or reader is created only on first use and then cached for
    the life of the bus.  ``take_json`` is intentionally non-blocking.
    """

    def __init__(
        self,
        domain_id: int = 0,
        *,
        participant: DomainParticipant | None = None,
        registry: TopicRegistry | Mapping[str, TopicSpec] = DEFAULT_TOPIC_REGISTRY,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        super().__init__(registry=registry, clock_ns=clock_ns)
        self._owns_participant = participant is None
        self._participant: DomainParticipant | None = (
            DomainParticipant(domain_id) if participant is None else participant
        )
        self._topics: dict[str, Topic] = {}
        self._writers: dict[str, DataWriter] = {}
        self._readers: dict[str, DataReader] = {}
        self._lock = RLock()

    @property
    def participant(self) -> DomainParticipant:
        self._ensure_open()
        assert self._participant is not None
        return self._participant

    def _topic_for(self, spec: TopicSpec) -> Topic:
        with self._lock:
            self._ensure_open()
            topic = self._topics.get(spec.name)
            if topic is None:
                topic = Topic(self.participant, spec.name, DdsEnvelope)
                self._topics[spec.name] = topic
            return topic

    def _writer_for(self, spec: TopicSpec) -> DataWriter:
        with self._lock:
            self._ensure_open()
            writer = self._writers.get(spec.name)
            if writer is None:
                writer = DataWriter(
                    self.participant, self._topic_for(spec), qos=qos_for(spec.profile)
                )
                self._writers[spec.name] = writer
            return writer

    def _reader_for(self, spec: TopicSpec) -> DataReader:
        with self._lock:
            self._ensure_open()
            reader = self._readers.get(spec.name)
            if reader is None:
                reader = DataReader(
                    self.participant, self._topic_for(spec), qos=qos_for(spec.profile)
                )
                self._readers[spec.name] = reader
            return reader

    def publish_json(
        self,
        topic: str | TopicSpec,
        payload: str | Mapping[str, Any],
        *,
        source_id: str,
        sequence: int,
        capture_time_ns: int | None = None,
        publish_time_ns: int | None = None,
        schema_version: int | None = None,
    ) -> DdsEnvelope:
        """Canonicalize and publish one JSON domain object."""

        spec, envelope = self._prepare_envelope(
            topic,
            payload,
            source_id=source_id,
            sequence=sequence,
            capture_time_ns=capture_time_ns,
            publish_time_ns=publish_time_ns,
            schema_version=schema_version,
        )
        self._writer_for(spec).write(envelope)
        return envelope

    def take_json(self, topic: str | TopicSpec, max_samples: int = 1) -> list[DdsEnvelope]:
        """Take up to ``max_samples`` envelopes without blocking."""

        self._ensure_open()
        _validate_max_samples(max_samples)
        spec = self._registry.resolve(topic)
        samples = self._reader_for(spec).take(N=max_samples)
        return [self._validate_received(spec, sample) for sample in samples]

    @staticmethod
    def _close_if_supported(entity: object) -> None:
        close = getattr(entity, "close", None)
        if callable(close):
            close()

    def close(self) -> None:
        """Release cached entities and an internally-created participant."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            for cache in (self._readers, self._writers, self._topics):
                for entity in cache.values():
                    self._close_if_supported(entity)
                cache.clear()
            participant = self._participant
            self._participant = None
            if self._owns_participant and participant is not None:
                self._close_if_supported(participant)


class InMemoryBus(_JsonBusBase):
    """Thread-safe simulation backend with the same public API as Cyclone DDS.

    Its per-topic deque applies the profile's keep-last depth, and storing the
    serialized IDL bytes prevents callers from mutating an enqueued sample.
    """

    def __init__(
        self,
        *,
        registry: TopicRegistry | Mapping[str, TopicSpec] = DEFAULT_TOPIC_REGISTRY,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        super().__init__(registry=registry, clock_ns=clock_ns)
        self._queues: dict[str, deque[bytes]] = {}
        self._lock = RLock()

    @staticmethod
    def _history_depth(spec: TopicSpec) -> int:
        history = qos_for(spec.profile)[Policy.History]
        depth = getattr(history, "depth", None)
        if not isinstance(depth, int) or depth < 1:
            raise RuntimeError(f"profile {spec.profile.value!r} is not keep-last")
        return depth

    def _queue_for(self, spec: TopicSpec) -> deque[bytes]:
        queue = self._queues.get(spec.name)
        if queue is None:
            queue = deque()
            self._queues[spec.name] = queue
        return queue

    def _append_with_per_source_history(
        self, spec: TopicSpec, envelope: DdsEnvelope, encoded: bytes
    ) -> None:
        queue = self._queue_for(spec)
        depth = self._history_depth(spec)
        same_source_indices = [
            index
            for index, item in enumerate(queue)
            if DdsEnvelope.deserialize(item).source_id == envelope.source_id
        ]
        overflow = len(same_source_indices) - depth + 1
        if overflow > 0:
            remove_indices = set(same_source_indices[:overflow])
            retained = [item for index, item in enumerate(queue) if index not in remove_indices]
            queue.clear()
            queue.extend(retained)
        queue.append(encoded)

    def publish_json(
        self,
        topic: str | TopicSpec,
        payload: str | Mapping[str, Any],
        *,
        source_id: str,
        sequence: int,
        capture_time_ns: int | None = None,
        publish_time_ns: int | None = None,
        schema_version: int | None = None,
    ) -> DdsEnvelope:
        spec, envelope = self._prepare_envelope(
            topic,
            payload,
            source_id=source_id,
            sequence=sequence,
            capture_time_ns=capture_time_ns,
            publish_time_ns=publish_time_ns,
            schema_version=schema_version,
        )
        encoded = DdsEnvelope.serialize(envelope)
        with self._lock:
            self._ensure_open()
            self._append_with_per_source_history(spec, envelope, encoded)
        return envelope

    def take_json(self, topic: str | TopicSpec, max_samples: int = 1) -> list[DdsEnvelope]:
        self._ensure_open()
        _validate_max_samples(max_samples)
        spec = self._registry.resolve(topic)
        samples: list[DdsEnvelope] = []
        with self._lock:
            self._ensure_open()
            queue = self._queue_for(spec)
            for _ in range(min(max_samples, len(queue))):
                samples.append(DdsEnvelope.deserialize(queue.popleft()))
        return [self._validate_received(spec, sample) for sample in samples]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queues.clear()


__all__ = [
    "AUDIO_EVENT_TOPIC",
    "DEFAULT_TOPIC_REGISTRY",
    "DRIVER_STATE_TOPIC",
    "ENVIRONMENT_STATE_TOPIC",
    "EXTERNAL_PERCEPTION_TOPIC",
    "HMI_COMMAND_TOPIC",
    "INS_STATE_TOPIC",
    "NODE_HEALTH_TOPIC",
    "RISK_EVENT_TOPIC",
    "SITUATION_SNAPSHOT_TOPIC",
    "TOPICS",
    "TOPIC_REGISTRY",
    "VEHICLE_STATE_TOPIC",
    "BusClosedError",
    "CycloneDdsBus",
    "InMemoryBus",
    "SchemaVersionError",
    "TopicRegistry",
    "TopicSpec",
    "UnknownTopicError",
]
