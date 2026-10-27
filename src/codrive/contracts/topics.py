"""Versioned DDS topic contracts shared by every CODRIVE node.

This module contains names and traffic-class identifiers only.  Concrete
Cyclone DDS QoS objects remain in :mod:`codrive.transport.qos` so importing a
contract never starts transport or hardware code.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final


class QoSProfile(str, Enum):
    """Stable traffic-class identifier used by a topic contract."""

    SENSOR_LATEST = "sensor_latest"
    STATE_LATEST = "state_latest"
    EVENT_LATEST = "event_latest"
    CONTEXT_STATE = "context_state"
    CRITICAL_EVENT = "critical_event"
    HMI_LATEST = "hmi_latest"
    HEALTH = "health"


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """Wire name, traffic profile, and payload schema version for one topic."""

    name: str
    profile: QoSProfile
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("topic name must be a non-empty string")
        if self.name != self.name.strip() or any(char.isspace() for char in self.name):
            raise ValueError("topic name must not contain whitespace")
        if not isinstance(self.profile, QoSProfile):
            try:
                object.__setattr__(self, "profile", QoSProfile(self.profile))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unknown QoS profile: {self.profile!r}") from exc
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise TypeError("schema_version must be an integer")
        if not 1 <= self.schema_version <= (1 << 32) - 1:
            raise ValueError("schema_version must be in [1, 4294967295]")


class TopicRegistry(Mapping[str, TopicSpec]):
    """Immutable registry resolving both short keys and full DDS topic names."""

    def __init__(self, specs: Mapping[str, TopicSpec] | Iterable[tuple[str, TopicSpec]]):
        entries = dict(specs)
        by_name: dict[str, TopicSpec] = {}
        normalized: dict[str, TopicSpec] = {}
        for key, spec in entries.items():
            if not isinstance(key, str) or not key.strip() or key != key.strip():
                raise ValueError("topic registry keys must be non-empty stripped strings")
            if not isinstance(spec, TopicSpec):
                raise TypeError(f"topic {key!r} does not contain a TopicSpec")
            if spec.name in by_name:
                raise ValueError(f"duplicate DDS topic name: {spec.name!r}")
            normalized[key] = spec
            by_name[spec.name] = spec
        self._specs = MappingProxyType(normalized)
        self._by_name = MappingProxyType(by_name)

    def __getitem__(self, key: str) -> TopicSpec:
        return self._specs[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def resolve(self, topic: str | TopicSpec) -> TopicSpec:
        if isinstance(topic, TopicSpec):
            registered = self._by_name.get(topic.name)
            if registered == topic:
                return registered
            raise UnknownTopicError(f"unregistered topic: {topic.name}")
        if not isinstance(topic, str):
            raise TypeError("topic must be a registry key, DDS name, or TopicSpec")
        spec = self._specs.get(topic) or self._by_name.get(topic)
        if spec is None:
            raise UnknownTopicError(f"unregistered topic: {topic}")
        return spec


class UnknownTopicError(KeyError):
    """Raised before DDS entity creation when a topic is not registered."""


_DEFAULT_TOPIC_SPECS: Final[dict[str, TopicSpec]] = {
    "external_perception": TopicSpec("codrive/v1/perception/external", QoSProfile.SENSOR_LATEST),
    "driver_state": TopicSpec("codrive/v1/perception/driver", QoSProfile.SENSOR_LATEST),
    "audio_event": TopicSpec("codrive/v1/perception/audio", QoSProfile.EVENT_LATEST),
    "environment_state": TopicSpec("codrive/v1/environment/state", QoSProfile.CONTEXT_STATE),
    "vehicle_state": TopicSpec("codrive/v1/vehicle/state", QoSProfile.SENSOR_LATEST),
    "ins_state": TopicSpec("codrive/v1/localization/ins", QoSProfile.SENSOR_LATEST),
    "situation_snapshot": TopicSpec("codrive/v1/fusion/snapshot", QoSProfile.STATE_LATEST),
    "risk_event": TopicSpec("codrive/v1/risk/event", QoSProfile.CRITICAL_EVENT),
    "hmi_command": TopicSpec("codrive/v1/hmi/command", QoSProfile.HMI_LATEST),
    "node_health": TopicSpec("codrive/v1/system/health", QoSProfile.HEALTH),
}

DEFAULT_TOPIC_REGISTRY: Final[TopicRegistry] = TopicRegistry(_DEFAULT_TOPIC_SPECS)
TOPIC_REGISTRY = DEFAULT_TOPIC_REGISTRY
TOPICS = DEFAULT_TOPIC_REGISTRY

EXTERNAL_PERCEPTION_TOPIC = DEFAULT_TOPIC_REGISTRY["external_perception"]
DRIVER_STATE_TOPIC = DEFAULT_TOPIC_REGISTRY["driver_state"]
AUDIO_EVENT_TOPIC = DEFAULT_TOPIC_REGISTRY["audio_event"]
ENVIRONMENT_STATE_TOPIC = DEFAULT_TOPIC_REGISTRY["environment_state"]
VEHICLE_STATE_TOPIC = DEFAULT_TOPIC_REGISTRY["vehicle_state"]
INS_STATE_TOPIC = DEFAULT_TOPIC_REGISTRY["ins_state"]
SITUATION_SNAPSHOT_TOPIC = DEFAULT_TOPIC_REGISTRY["situation_snapshot"]
RISK_EVENT_TOPIC = DEFAULT_TOPIC_REGISTRY["risk_event"]
HMI_COMMAND_TOPIC = DEFAULT_TOPIC_REGISTRY["hmi_command"]
NODE_HEALTH_TOPIC = DEFAULT_TOPIC_REGISTRY["node_health"]


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
    "TOPIC_REGISTRY",
    "TOPICS",
    "VEHICLE_STATE_TOPIC",
    "QoSProfile",
    "TopicRegistry",
    "TopicSpec",
    "UnknownTopicError",
]
