"""Heartbeat monitoring shared by the Pi supervisor and test harness."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..contracts.models import NodeHealth


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    expected_nodes: tuple[str, ...] = ()
    heartbeat_period_ms: int = 500
    missed_before_degraded: int = 3

    @property
    def timeout_ns(self) -> int:
        return self.heartbeat_period_ms * self.missed_before_degraded * 1_000_000


@dataclass(slots=True)
class NodeHealthMonitor:
    policy: HealthPolicy = field(default_factory=HealthPolicy)
    _last_seen_ns: dict[str, int] = field(default_factory=dict)
    _latest: dict[str, NodeHealth] = field(default_factory=dict)

    def ingest(self, health: NodeHealth, *, received_time_ns: int | None = None) -> bool:
        now_ns = time.time_ns() if received_time_ns is None else received_time_ns
        if health.header.source_id != health.node_id:
            return False
        previous = self._latest.get(health.node_id)
        if previous is not None:
            if (
                previous.header.boot_id == health.header.boot_id
                and health.header.sequence <= previous.header.sequence
            ):
                return False
            if (
                previous.header.boot_id != health.header.boot_id
                and health.header.capture_time_ns < previous.header.capture_time_ns
            ):
                return False
        self._latest[health.node_id] = health
        self._last_seen_ns[health.node_id] = now_ns
        return True

    def missing_nodes(self, *, now_ns: int | None = None) -> tuple[str, ...]:
        now_ns = time.time_ns() if now_ns is None else now_ns
        return tuple(
            node_id
            for node_id in self.policy.expected_nodes
            if node_id not in self._last_seen_ns
            or now_ns - self._last_seen_ns[node_id] > self.policy.timeout_ns
        )
