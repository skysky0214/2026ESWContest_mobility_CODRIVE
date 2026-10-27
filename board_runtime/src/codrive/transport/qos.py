"""Named Cyclone DDS QoS profiles for CODRIVE traffic classes."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping

from cyclonedds.qos import Policy, Qos
from cyclonedds.util import duration


class QoSProfile(str, Enum):
    """Stable profile identifiers stored in :class:`TopicSpec` objects."""

    SENSOR_LATEST = "sensor_latest"
    STATE_LATEST = "state_latest"
    EVENT_LATEST = "event_latest"
    CONTEXT_STATE = "context_state"
    CRITICAL_EVENT = "critical_event"
    HMI_LATEST = "hmi_latest"
    HEALTH = "health"


# High-rate inference outputs: old samples are actively harmful to live fusion.
SENSOR_LATEST: Final[Qos] = Qos(
    Policy.History.KeepLast(1),
    Policy.Reliability.BestEffort,
    Policy.Durability.Volatile,
    Policy.Deadline(duration(milliseconds=100)),
    Policy.Lifespan(duration(milliseconds=250)),
)

# Fused snapshots: keep only the newest state but deliver it reliably.
STATE_LATEST: Final[Qos] = Qos(
    Policy.History.KeepLast(1),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=20)),
    Policy.Durability.Volatile,
    Policy.Deadline(duration(milliseconds=100)),
    Policy.Lifespan(duration(milliseconds=250)),
)

# Sparse detections such as sirens are reliable, but silence is normal so there
# is deliberately no deadline policy.
EVENT_LATEST: Final[Qos] = Qos(
    Policy.History.KeepLast(8),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=10)),
    Policy.Durability.Volatile,
    Policy.Lifespan(duration(seconds=1)),
)

# Slow weather/time context survives a late join without generating false
# deadline alarms between its 60-second publications.
CONTEXT_STATE: Final[Qos] = Qos(
    Policy.History.KeepLast(1),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=20)),
    Policy.Durability.TransientLocal,
    Policy.Lifespan(duration(seconds=310)),
)

# Risk events retain a short bounded history for logging/diagnostics.
CRITICAL_EVENT: Final[Qos] = Qos(
    Policy.History.KeepLast(16),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=10)),
    Policy.Durability.TransientLocal,
    Policy.Lifespan(duration(seconds=2)),
)

# An HMI must never replay a backlog of warnings after restarting.
HMI_LATEST: Final[Qos] = Qos(
    Policy.History.KeepLast(1),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=10)),
    Policy.Durability.TransientLocal,
    Policy.Lifespan(duration(seconds=2)),
)

# A late joiner should immediately see the latest node health, while deadline and
# lifespan make silent or stale nodes observable.
HEALTH: Final[Qos] = Qos(
    Policy.History.KeepLast(1),
    Policy.Reliability.Reliable(max_blocking_time=duration(milliseconds=20)),
    Policy.Durability.TransientLocal,
    Policy.Deadline(duration(seconds=2)),
    Policy.Lifespan(duration(seconds=6)),
)


QOS_PROFILES: Final[Mapping[QoSProfile, Qos]] = MappingProxyType(
    {
        QoSProfile.SENSOR_LATEST: SENSOR_LATEST,
        QoSProfile.STATE_LATEST: STATE_LATEST,
        QoSProfile.EVENT_LATEST: EVENT_LATEST,
        QoSProfile.CONTEXT_STATE: CONTEXT_STATE,
        QoSProfile.CRITICAL_EVENT: CRITICAL_EVENT,
        QoSProfile.HMI_LATEST: HMI_LATEST,
        QoSProfile.HEALTH: HEALTH,
    }
)


def qos_for(profile: QoSProfile | str) -> Qos:
    """Resolve an enum or its string value to a concrete Cyclone DDS ``Qos``."""

    try:
        normalized = profile if isinstance(profile, QoSProfile) else QoSProfile(profile)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown QoS profile: {profile!r}") from exc
    return QOS_PROFILES[normalized]


# Conventional spelling retained for callers that do not use the acronym style.
QosProfile = QoSProfile


__all__ = [
    "CRITICAL_EVENT",
    "CONTEXT_STATE",
    "EVENT_LATEST",
    "HEALTH",
    "HMI_LATEST",
    "QOS_PROFILES",
    "QoSProfile",
    "QosProfile",
    "SENSOR_LATEST",
    "STATE_LATEST",
    "qos_for",
]
