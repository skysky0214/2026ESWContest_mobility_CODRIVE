import json

from codrive.apps.local_bus import LocalBus
from codrive.apps.standby import run_standby
from codrive.health import HealthPolicy, NodeHealthMonitor
from codrive.models import Header, NodeHealth, NodeState
from codrive.runtime import decode_model, publish_model


def test_standby_reports_real_peer_receipt_without_publishing_perception():
    class RecordingBus(LocalBus):
        def __init__(self):
            super().__init__()
            self.published = []

        def publish_json(self, topic, payload, **kwargs):
            wire = super().publish_json(topic, payload, **kwargs)
            self.published.append((topic, wire))
            return wire

    bus = RecordingBus()
    for name in ("pi5.hmi", "jetson-cabin"):
        publish_model(bus, "node_health", NodeHealth(
            Header.now(name, 1), name, NodeState.READY, 1,
        ))
    run_standby(bus, duration=0.02)
    health = [decode_model(wire, NodeHealth) for topic, wire in bus.published
              if topic == "node_health"]
    active = next(sample for sample in health if sample.detail.startswith("{"))
    detail = json.loads(active.detail)
    assert active.node_id == active.header.source_id == "jetson-front"
    assert active.state is NodeState.DEGRADED
    assert detail["camera_enabled"] is False
    assert detail["peers"] == {"pi5.hmi": True, "jetson-cabin": True}
    monitor = NodeHealthMonitor(HealthPolicy(expected_nodes=("jetson-front",)))
    assert monitor.ingest(active)
    assert monitor.missing_nodes() == ()
    assert all(topic != "external_perception" for topic, _ in bus.published)
