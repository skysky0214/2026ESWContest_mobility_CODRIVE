from __future__ import annotations

import json

from codrive.fusion import FusionPolicy, SituationFusion
from codrive.models import (
    ExternalPerception,
    Header,
    Severity,
    TrafficLight,
    VehicleState,
    to_json,
)
from codrive.runtime import JetsonPerceptionPublisher, PiOrchestrator, decode_model, publish_model
from codrive.transport import InMemoryBus


NOW_NS = 3_000_000_000_000


def test_in_memory_multi_node_pipeline_produces_hmi() -> None:
    bus = InMemoryBus(clock_ns=lambda: NOW_NS)
    vehicle = VehicleState(
        Header.now("pi5.panda", 1, capture_time_ns=NOW_NS),
        speed_mps=40 / 3.6,
        steering_wheel_deg=0,
        brake_pressed=False,
        accelerator_pct=10,
    )
    external = ExternalPerception(
        Header.now("jetson-front.front", 1, frame_id="front", capture_time_ns=NOW_NS),
        camera_id="front",
        model_version="test",
        traffic_light=TrafficLight.RED,
        traffic_light_confidence=0.99,
    )
    publish_model(bus, "vehicle_state", vehicle)
    JetsonPerceptionPublisher(bus, "jetson-front").external(external)
    orchestrator = PiOrchestrator(
        bus,
        fusion=SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",))),
    )

    result = orchestrator.step(now_ns=NOW_NS)

    assert result.risk is not None and result.risk.severity is Severity.WARNING
    assert result.hmi is not None and result.hmi.display_text == "빨간불! 제동"
    risk_wire = bus.take_json("risk_event", max_samples=1)[0]
    assert decode_model(risk_wire, type(result.risk)) == result.risk
    assert bus.take_json("hmi_command", max_samples=1)


def test_worker_rejects_spoofed_source_id() -> None:
    bus = InMemoryBus(clock_ns=lambda: NOW_NS)
    publisher = JetsonPerceptionPublisher(bus, "jetson-front")
    sample = ExternalPerception(
        Header.now("another-worker", 1, capture_time_ns=NOW_NS),
        camera_id="front",
        model_version="test",
    )
    try:
        publisher.external(sample)
    except ValueError as exc:
        assert "cannot publish" in str(exc)
    else:
        raise AssertionError("spoofed source was accepted")


def test_bad_wire_primitive_is_rejected_without_crashing_orchestrator() -> None:
    bus = InMemoryBus(clock_ns=lambda: NOW_NS)
    sample = VehicleState(
        Header.now("pi5.panda", 1, capture_time_ns=NOW_NS),
        speed_mps=0.0,
        steering_wheel_deg=0.0,
        brake_pressed=True,
        accelerator_pct=0.0,
    )
    payload = json.loads(to_json(sample))
    payload["speed_mps"] = "fast"
    bus.publish_json(
        "vehicle_state",
        payload,
        source_id=sample.header.source_id,
        sequence=sample.header.sequence,
        capture_time_ns=sample.header.capture_time_ns,
        publish_time_ns=sample.header.publish_time_ns,
    )
    orchestrator = PiOrchestrator(
        bus,
        fusion=SituationFusion(policy=FusionPolicy(expected_camera_ids=())),
    )

    result = orchestrator.step(now_ns=NOW_NS)

    assert result.rejected_samples == 1
    assert result.snapshot.vehicle is None
