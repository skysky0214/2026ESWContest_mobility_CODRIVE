from codrive.health import HealthPolicy, NodeHealthMonitor
from codrive.models import Header, NodeHealth, NodeState


NOW_NS = 4_000_000_000_000


def test_health_monitor_marks_node_missing_after_three_periods() -> None:
    monitor = NodeHealthMonitor(
        HealthPolicy(
            expected_nodes=("jetson-front", "jetson-cabin"),
            heartbeat_period_ms=500,
            missed_before_degraded=3,
        )
    )
    front = NodeHealth(
        Header.now("jetson-front", 1, capture_time_ns=NOW_NS),
        node_id="jetson-front",
        state=NodeState.READY,
        uptime_s=10.0,
    )

    assert monitor.ingest(front, received_time_ns=NOW_NS)
    assert monitor.missing_nodes(now_ns=NOW_NS) == ("jetson-cabin",)
    assert set(monitor.missing_nodes(now_ns=NOW_NS + 1_500_000_001)) == {
        "jetson-front",
        "jetson-cabin",
    }


def test_health_source_must_match_node_id() -> None:
    monitor = NodeHealthMonitor()
    spoofed = NodeHealth(
        Header.now("unknown", 1, capture_time_ns=NOW_NS),
        node_id="jetson-front",
        state=NodeState.READY,
        uptime_s=1.0,
    )
    assert not monitor.ingest(spoofed, received_time_ns=NOW_NS)


def test_camera_disabled_worker_can_be_connected_and_then_expire() -> None:
    monitor = NodeHealthMonitor(HealthPolicy(expected_nodes=("jetson-front",)))
    front = NodeHealth(
        Header.now("jetson-front", 1, capture_time_ns=NOW_NS),
        "jetson-front", NodeState.DEGRADED, 1, detail="camera_disabled",
    )
    monitor.ingest(front, received_time_ns=NOW_NS)
    status = monitor.statuses(now_ns=NOW_NS + 500_000_000)["jetson-front"]
    assert status["connected"] is True
    assert status["state"] == "degraded"
    assert status["age_ms"] == 500
    assert monitor.statuses(now_ns=NOW_NS + 1_500_000_001)["jetson-front"]["connected"] is False
