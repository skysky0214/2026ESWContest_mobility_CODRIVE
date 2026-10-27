"""Bounded JSON loopback for local DMS verification without CycloneDDS.

This transport is local only; it does not emulate DDS QoS or wire serialization.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocalEnvelope:
    source_id: str
    sequence: int
    capture_time_ns: int
    publish_time_ns: int
    schema_version: int
    payload_json: str


class LocalBus:
    def __init__(self, *, clock_ns=time.time_ns):
        self.clock_ns = clock_ns
        self.queues = defaultdict(lambda: deque(maxlen=16))
        self.closed = False

    def publish_json(
        self,
        topic,
        payload,
        *,
        source_id,
        sequence,
        capture_time_ns=None,
        publish_time_ns=None,
        schema_version=None,
    ):
        if self.closed:
            raise RuntimeError("Local bus is closed")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise TypeError("Payload must be an object")
        now = self.clock_ns()
        envelope = LocalEnvelope(
            source_id,
            sequence,
            now if capture_time_ns is None else capture_time_ns,
            now if publish_time_ns is None else publish_time_ns,
            1 if schema_version is None else schema_version,
            payload,
        )
        self.queues[topic].append(envelope)
        return envelope

    def take_json(self, topic, max_samples=1):
        if self.closed:
            raise RuntimeError("Local bus is closed")
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        queue = self.queues[topic]
        return [queue.popleft() for _ in range(min(max_samples, len(queue)))]

    def close(self):
        self.closed = True
        self.queues.clear()
