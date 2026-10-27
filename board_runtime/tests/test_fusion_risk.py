from __future__ import annotations

from dataclasses import replace

from codrive.fusion import FusionPolicy, SituationFusion
from codrive.guidance import HmiArbiter, HmiPolicy
from codrive.models import (
    BoundingBox,
    DriverState,
    ExternalPerception,
    Header,
    InsState,
    Severity,
    TrackedObject,
    TrafficLight,
    TurnSignal,
    VehicleState,
)
from codrive.risk import RiskEngine


NOW_NS = 2_000_000_000_000


def header(source: str, sequence: int = 1, age_ms: int = 0) -> Header:
    return Header.now(
        source,
        sequence,
        frame_id=source,
        capture_time_ns=NOW_NS - age_ms * 1_000_000,
    )


def vehicle(**overrides: object) -> VehicleState:
    values: dict[str, object] = {
        "header": header("pi5.panda"),
        "speed_mps": 40 / 3.6,
        "steering_wheel_deg": 0.0,
        "brake_pressed": False,
        "accelerator_pct": 20.0,
        "turn_signal": TurnSignal.OFF,
    }
    values.update(overrides)
    return VehicleState(**values)  # type: ignore[arg-type]


def front_perception(*, ttc_s: float = 1.2, lane_offset_m: float = -0.9) -> ExternalPerception:
    return ExternalPerception(
        header=header("jetson-front"),
        camera_id="front",
        model_version="test",
        objects=(
            TrackedObject(
                1,
                "pedestrian",
                0.95,
                BoundingBox(0.4, 0.2, 0.6, 0.9),
                distance_m=10.0,
                ttc_s=ttc_s,
            ),
        ),
        traffic_light=TrafficLight.GREEN,
        lane_offset_m=lane_offset_m,
        lane_confidence=0.9,
    )


def test_fusion_drops_duplicate_and_stale_samples() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    assert fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    assert not fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(front_perception(), received_time_ns=NOW_NS)

    current = fusion.snapshot(now_ns=NOW_NS)
    stale = fusion.snapshot(now_ns=NOW_NS + 300_000_000)

    assert current.vehicle is not None
    assert len(current.external) == 1
    assert stale.vehicle is None
    assert stale.external == ()
    assert "vehicle" in stale.stale_sources
    assert "camera:front" in stale.stale_sources


def test_fusion_sequences_are_per_stream_and_allow_new_boot() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=()))
    shared_source_vehicle = vehicle(
        header=Header.now("pi5.gateway", 1, boot_id="boot-a", capture_time_ns=NOW_NS)
    )
    shared_source_ins = InsState(
        Header.now("pi5.gateway", 1, boot_id="boot-a", capture_time_ns=NOW_NS)
    )
    restarted_vehicle = vehicle(
        header=Header.now("pi5.gateway", 0, boot_id="boot-b", capture_time_ns=NOW_NS + 1)
    )

    assert fusion.ingest(shared_source_vehicle, received_time_ns=NOW_NS)
    assert fusion.ingest(shared_source_ins, received_time_ns=NOW_NS)
    assert fusion.ingest(restarted_vehicle, received_time_ns=NOW_NS + 1)


def test_fusion_rejects_unlisted_camera_source() -> None:
    fusion = SituationFusion(
        policy=FusionPolicy(
            expected_camera_ids=("front",),
            source_allowlist=(("external:front", "jetson-front.front"),),
        )
    )
    spoofed = front_perception()
    assert not fusion.ingest(spoofed, received_time_ns=NOW_NS)


def test_pedestrian_ttc_wins_over_lane_departure() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(front_perception(), received_time_ns=NOW_NS)
    snapshot = fusion.snapshot(now_ns=NOW_NS)

    risk = RiskEngine().evaluate(snapshot, now_ns=NOW_NS)

    assert risk is not None
    assert risk.code == "pedestrian_collision"
    assert risk.severity is Severity.CRITICAL
    assert risk.recommended_action == "brake_now"
    command = HmiPolicy().render(risk)
    assert command.source == "template"
    assert command.display_text == "보행자! 즉시 제동"


def test_risk_engine_suppresses_duplicate_until_escalation() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    front = front_perception(ttc_s=2.5, lane_offset_m=0.0)
    front = replace(front, objects=(replace(front.objects[0], distance_m=30.0),))
    fusion.ingest(front, received_time_ns=NOW_NS)
    engine = RiskEngine()

    first = engine.evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)
    duplicate = engine.evaluate(
        fusion.snapshot(now_ns=NOW_NS + 100_000_000), now_ns=NOW_NS + 100_000_000
    )

    assert first is not None and first.severity is Severity.WARNING
    assert duplicate is None


def test_zero_confidence_pedestrian_does_not_trigger() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    weak = front_perception(lane_offset_m=0.0)
    weak_object = weak.objects[0]
    weak = replace(weak, objects=(replace(weak_object, confidence=0.0),))
    fusion.ingest(weak, received_time_ns=NOW_NS)

    assert RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS) is None


def test_can_and_ins_cross_check_unintended_acceleration() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=()))
    fusion.ingest(
        vehicle(accelerator_pct=92.0, header=header("pi5.panda", 2)),
        received_time_ns=NOW_NS,
    )
    fusion.ingest(
        InsState(header("pi5.ins"), longitudinal_accel_mps2=4.2),
        received_time_ns=NOW_NS,
    )

    event = RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)

    assert event is not None
    assert event.code == "unintended_acceleration"
    assert event.severity is Severity.CRITICAL


def test_driver_distraction_generates_short_hmi_text() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=()))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(
        DriverState(
            header("jetson-cabin"),
            camera_id="cabin",
            model_version="test",
            attention_score=0.2,
            looking_forward=False,
        ),
        received_time_ns=NOW_NS,
    )
    engine = RiskEngine()
    original_driver = fusion.snapshot(now_ns=NOW_NS).driver
    original_vehicle = fusion.snapshot(now_ns=NOW_NS).vehicle
    event = engine.evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)
    assert event is None
    for elapsed_ms in range(100, 3001, 100):
        now = NOW_NS + elapsed_ms * 1_000_000
        sample = replace(original_driver, header=replace(header("jetson-cabin", elapsed_ms + 1), capture_time_ns=now))
        fusion.ingest(sample, received_time_ns=now)
        fusion.ingest(replace(original_vehicle,
            header=replace(header("pi5.panda", elapsed_ms + 1), capture_time_ns=now)), received_time_ns=now)
        event = engine.evaluate(fusion.snapshot(now_ns=now), now_ns=now)
    assert event is not None
    command = HmiPolicy().render(event)
    assert len(command.display_text.split()) <= 4


def test_hmi_arbiter_rejects_expired_and_suppresses_repeated_tts() -> None:
    event = RiskEngine().evaluate(
        SituationFusion(policy=FusionPolicy(expected_camera_ids=())).snapshot(now_ns=NOW_NS),
        now_ns=NOW_NS,
    )
    assert event is None

    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(front_perception(), received_time_ns=NOW_NS)
    first_event = RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)
    assert first_event is not None
    policy = HmiPolicy()
    first = policy.render(first_event)
    arbiter = HmiArbiter()
    decision = arbiter.accept(first, now_ns=NOW_NS)
    assert decision is not None and decision.speak
    assert arbiter.accept(first, now_ns=NOW_NS + 1) is None
    repeated = replace(
        first,
        event_id="repeated-event",
        expires_at_ns=NOW_NS + 2_000_000_000,
    )
    repeated_decision = arbiter.accept(repeated, now_ns=NOW_NS + 1_000_000_000)
    assert repeated_decision is not None and not repeated_decision.speak
    assert arbiter.accept(first, now_ns=first.expires_at_ns) is None
