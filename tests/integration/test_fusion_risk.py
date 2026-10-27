from __future__ import annotations

from dataclasses import replace

from codrive.situation.fusion import FusionPolicy, SituationFusion
from codrive.guidance.policy import HmiArbiter, HmiPolicy
from codrive.contracts.models import (
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
        traffic_light=TrafficLight.RED,
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


def test_red_light_generates_hmi_without_ranging() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(replace(front_perception(), objects=(), lane_offset_m=None),
                  received_time_ns=NOW_NS)
    snapshot = fusion.snapshot(now_ns=NOW_NS)

    risk = RiskEngine().evaluate(snapshot, now_ns=NOW_NS)

    assert risk is not None
    assert risk.code == "red_light_approach"
    assert risk.severity is Severity.WARNING
    assert risk.recommended_action == "brake_now"
    command = HmiPolicy().render(risk)
    assert command.source == "template"
    assert command.display_text == "빨간불! 제동"


def test_risk_engine_suppresses_duplicate_signal() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    front = front_perception(ttc_s=2.5, lane_offset_m=0.0)
    front = replace(front, objects=(replace(front.objects[0], distance_m=15.0),))
    fusion.ingest(front, received_time_ns=NOW_NS)
    engine = RiskEngine()

    first = engine.evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)
    duplicate = engine.evaluate(
        fusion.snapshot(now_ns=NOW_NS + 100_000_000), now_ns=NOW_NS + 100_000_000
    )

    assert first is not None and first.code == "red_light_approach"
    assert duplicate is None


def test_object_detection_does_not_create_a_collision_risk() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    weak = replace(front_perception(lane_offset_m=0.0), traffic_light=TrafficLight.GREEN)
    weak_object = weak.objects[0]
    weak = replace(weak, objects=(replace(weak_object, confidence=0.0),))
    fusion.ingest(weak, received_time_ns=NOW_NS)

    assert RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS) is None


def test_ins_acceleration_is_excluded_from_risk() -> None:
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

    assert event is None


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
    event = RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)
    assert event is not None
    command = HmiPolicy().render(event)
    assert len(command.display_text.split()) <= 4


def test_driver_drowsiness_generates_dedicated_hmi_text() -> None:
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=()))
    fusion.ingest(vehicle(), received_time_ns=NOW_NS)
    fusion.ingest(
        DriverState(
            header("jetson-cabin.dms"),
            camera_id="cabin",
            model_version="dms-test",
            attention_score=0.8,
            looking_forward=True,
            drowsiness_score=0.9,
            microsleep_ms=1_600,
            eyes_closed=True,
        ),
        received_time_ns=NOW_NS,
    )

    event = RiskEngine().evaluate(fusion.snapshot(now_ns=NOW_NS), now_ns=NOW_NS)

    assert event is not None
    assert event.code == "driver_drowsiness"
    # Current main policy scores isolated drowsiness at 85, below emergency (90).
    assert event.severity is Severity.WARNING
    command = HmiPolicy().render(event)
    assert command.display_text == "안전하게 정차하세요"


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
