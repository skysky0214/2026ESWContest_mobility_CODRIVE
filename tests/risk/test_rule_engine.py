from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codrive.situation.fusion import FusionPolicy, SituationFusion
from codrive.contracts.models import (
    AudioEvent, AudioKind, BoundingBox, DriverState, ExternalPerception, Header,
    InsState, RiskAssessment, RiskCandidate, RiskLevel, SituationSnapshot,
    TrackedObject, TrafficLight, TurnSignal, VehicleState, from_json, to_json,
)
from codrive.apps.raspberry_pi.pipeline import DecisionPipeline
from codrive.risk import RiskEngine, RiskPolicy, load_risk_policy
from codrive.risk.fsm import RiskStateMachine
from codrive.simulation.scenarios import run_rule_scenarios


NOW = 3_000_000_000_000
ROOT = Path(__file__).resolve().parents[2]


def header(source: str, *, now: int = NOW, sequence: int = 1) -> Header:
    return Header.now(source, sequence, capture_time_ns=now)


def vehicle(**overrides) -> VehicleState:
    return replace(VehicleState(header("mock.vehicle"), 40 / 3.6, 0, False, 18), **overrides)


def driver(**overrides) -> DriverState:
    return replace(DriverState(header("mock.driver"), "cabin", "mock", 0.95, True), **overrides)


def obj(**overrides) -> TrackedObject:
    return replace(TrackedObject(1, "pedestrian", 0.95, BoundingBox(0.4, 0.2, 0.6, 0.9),
                                 distance_m=15), **overrides)


def external(**overrides) -> ExternalPerception:
    return replace(ExternalPerception(header("mock.external"), "front", "mock"), **overrides)


def snapshot(**overrides) -> SituationSnapshot:
    return replace(SituationSnapshot(
        header("mock.snapshot"), vehicle=vehicle(), driver=driver(),
        external=(external(),), ins=InsState(header("mock.ins"), longitudinal_accel_mps2=0),
    ), **overrides)


def assess(**overrides) -> RiskAssessment:
    return RiskEngine().assess(snapshot(**overrides), now_ns=NOW)


@pytest.mark.parametrize("score,level", [
    (0, RiskLevel.NORMAL), (29.99, RiskLevel.NORMAL), (30, RiskLevel.CAUTION),
    (59.99, RiskLevel.CAUTION), (60, RiskLevel.WARNING),
    (89.99, RiskLevel.WARNING), (90, RiskLevel.EMERGENCY),
])
def test_score_boundaries(score, level):
    assert RiskEngine().level_for_score(score) is level


@pytest.mark.parametrize("speed,brake,level", [
    (0, True, RiskLevel.NORMAL), (0, False, RiskLevel.NORMAL),
    (40 / 3.6, False, RiskLevel.WARNING), (40 / 3.6, True, RiskLevel.NORMAL),
])
def test_red_light_depends_on_vehicle_behavior(speed, brake, level):
    result = assess(vehicle=vehicle(speed_mps=speed, brake_pressed=brake), external=(
        external(traffic_light=TrafficLight.RED, traffic_light_confidence=0.95),
    ))
    assert result.raw_level is level


@pytest.mark.parametrize("speed,direction,level", [
    (0, "rear", RiskLevel.CAUTION), (60 / 3.6, "unknown", RiskLevel.CAUTION),
    (60 / 3.6, "rear", RiskLevel.WARNING),
])
def test_siren_context(speed, direction, level):
    audio = AudioEvent(header("mock.audio"), AudioKind.SIREN, 0.95, 960, "mock", direction)
    result = assess(vehicle=vehicle(speed_mps=speed), audio=audio)
    assert result.raw_level is level


def test_rear_camera_signal_does_not_govern_forward_driving():
    result = assess(external=(external(camera_id="rear", traffic_light=TrafficLight.RED,
                                        traffic_light_confidence=0.95),))
    assert result.raw_level is RiskLevel.NORMAL


def test_invalid_can_and_stale_inputs_are_excluded():
    road = (external(traffic_light=TrafficLight.RED, traffic_light_confidence=0.95),)
    assert assess(vehicle=vehicle(can_valid=False), external=road).raw_level is RiskLevel.NORMAL
    stale = external(header=header("mock.external", now=NOW - 300_000_000),
                     objects=(obj(ttc_s=0.1),))
    result = assess(external=(stale,))
    assert result.raw_level is RiskLevel.NORMAL
    assert result.degraded


def test_face_missing_is_unavailable_not_inattention():
    result = assess(driver=driver(face_detected=False, looking_forward=False, attention_score=0))
    assert result.raw_level is RiskLevel.NORMAL
    assert result.degraded
    assert "driver_context" in result.unavailable_rules


def test_sustained_eye_closure_requires_new_observations():
    engine = RiskEngine()
    closed = driver(eyes_closed=True)
    assert engine.assess(snapshot(driver=closed), now_ns=NOW).raw_level is RiskLevel.NORMAL
    repeated = engine.assess(snapshot(driver=closed), now_ns=NOW + 300_000_000)
    assert repeated.raw_level is RiskLevel.NORMAL
    result = None
    for index in range(1, 4):
        captured = NOW + index * 300_000_000
        observed = replace(closed, header=header("mock.driver", now=captured, sequence=index + 1))
        result = engine.assess(snapshot(driver=observed), now_ns=captured)
    assert result.raw_level is RiskLevel.WARNING
    assert result.selected_risk.code == "driver_drowsiness"


def candidate(score, *, target="front:1", code="object_collision") -> RiskCandidate:
    return RiskCandidate(code, target, RiskEngine().level_for_score(score), score,
                         "slow_down", "object_slow", ())


def test_fsm_hysteresis_does_not_flap_around_warning_entry():
    fsm = RiskStateMachine()
    for index, score in enumerate((61, 58, 62, 59)):
        level, active, held = fsm.update(candidate(score), now_ns=NOW + index * 100_000_000)
        assert level is RiskLevel.WARNING
        assert active is not None
    assert held
    assert fsm.update(candidate(40), now_ns=NOW + 400_000_000)[0] is RiskLevel.WARNING
    assert fsm.update(candidate(40), now_ns=NOW + 900_000_000)[0] is RiskLevel.CAUTION


def test_fsm_emergency_is_immediate_and_new_target_does_not_inherit_level():
    fsm = RiskStateMachine()
    fsm.update(candidate(60), now_ns=NOW)
    assert fsm.update(candidate(95), now_ns=NOW + 1)[0] is RiskLevel.EMERGENCY
    level, active, held = fsm.update(candidate(35, target="rear:2"), now_ns=NOW + 2)
    assert level is RiskLevel.EMERGENCY and active.target_id == "front:1" and held
    level, active, held = fsm.update(candidate(35, target="rear:2"), now_ns=NOW + 500_000_002)
    assert level is RiskLevel.CAUTION and active.target_id == "rear:2" and not held


def test_fsm_returns_normal_without_a_normal_risk_event():
    fsm = RiskStateMachine()
    fsm.update(candidate(35), now_ns=NOW)
    fsm.update(candidate(10), now_ns=NOW + 1)
    level, active, held = fsm.update(candidate(10), now_ns=NOW + 500_000_001)
    assert level is RiskLevel.NORMAL and active is None and not held


def test_fsm_does_not_revive_old_warning_after_a_processing_gap():
    fsm = RiskStateMachine()
    fsm.update(candidate(95), now_ns=NOW)
    level, active, held = fsm.update(None, now_ns=NOW + 2_000_000_000)
    assert level is RiskLevel.NORMAL and active is None and not held


def test_assessment_survives_event_cooldown_and_clears_explicitly():
    engine = RiskEngine()
    sample = snapshot(external=(external(traffic_light=TrafficLight.RED),))
    first = engine.assess(sample, now_ns=NOW)
    repeated = engine.assess(sample, now_ns=NOW + 50_000_000)
    assert first.event is not None and repeated.event is None
    assert repeated.stable_level is RiskLevel.WARNING
    assert repeated.active_risk is not None
    engine.assess(snapshot(external=()), now_ns=NOW + 100_000_000)
    cleared = engine.assess(snapshot(external=()), now_ns=NOW + 600_000_000)
    assert cleared.stable_level is RiskLevel.NORMAL
    assert cleared.active_risk is None and cleared.event is None


def test_escalation_and_different_signal_source_bypass_cooldown():
    engine = RiskEngine()
    road = external(traffic_light=TrafficLight.RED)
    first = engine.assess(snapshot(external=(road,)), now_ns=NOW)
    assert first.event is not None
    urgent = snapshot(external=(road,), driver=driver(phone_detected=True))
    second = engine.assess(urgent, now_ns=NOW + 1)
    assert second.event is not None and second.stable_level is RiskLevel.EMERGENCY
    another = replace(road, header=header("another.front"))
    third = engine.assess(replace(urgent, external=(another,)), now_ns=NOW + 2)
    assert third.event is not None


def test_assessment_json_round_trip():
    result = assess(external=(external(traffic_light=TrafficLight.RED),))
    assert from_json(RiskAssessment, to_json(result)) == result


def test_reescalation_bypasses_cooldown_even_if_demotion_did_not_emit():
    engine = RiskEngine(policy=RiskPolicy(release_hold_ms=0, red_light_base_score=60,
                                         red_light_speed_bonus=40))
    road = (external(traffic_light=TrafficLight.RED),)
    urgent = snapshot(external=road)
    lower = snapshot(external=road, vehicle=vehicle(speed_mps=20 / 3.6))
    assert engine.assess(urgent, now_ns=NOW).event is not None
    reduced = engine.assess(lower, now_ns=NOW + 1)
    assert reduced.stable_level is RiskLevel.WARNING and reduced.event is None
    escalated = engine.assess(urgent, now_ns=NOW + 2)
    assert escalated.stable_level is RiskLevel.EMERGENCY and escalated.event is not None


def test_pipeline_runs_without_transport_and_preserves_rejection_count():
    pipeline = DecisionPipeline()
    for sample in (vehicle(), driver(), external(traffic_light=TrafficLight.RED)):
        assert pipeline.ingest(sample, received_time_ns=NOW)
    assert not pipeline.ingest(vehicle(), received_time_ns=NOW)
    result = pipeline.step(now_ns=NOW)
    assert result.assessment.stable_level is RiskLevel.WARNING
    assert result.hmi.display_text == "빨간불! 제동"
    assert result.rejected_samples == 1
    assert not result.assessment.degraded


def test_fusion_future_sample_keeps_previous_observation_available():
    fusion = SituationFusion()
    current = vehicle()
    future = vehicle(header=header("mock.vehicle", now=NOW + 20_000_000, sequence=2))
    assert fusion.ingest(current, received_time_ns=NOW)
    assert fusion.ingest(future, received_time_ns=NOW)
    assert fusion.snapshot(now_ns=NOW).vehicle == current
    assert fusion.snapshot(now_ns=NOW + 20_000_000).vehicle == future


def test_fusion_history_is_bounded_and_rejects_capture_regression():
    fusion = SituationFusion(policy=FusionPolicy(history_depth=2))
    for index in range(3):
        fusion.ingest(vehicle(header=header("mock.vehicle", now=NOW + index, sequence=index)),
                      received_time_ns=NOW + index)
    assert fusion.snapshot(now_ns=NOW).vehicle is None
    regression = vehicle(header=header("mock.vehicle", now=NOW, sequence=4))
    assert not fusion.ingest(regression, received_time_ns=NOW + 3)


def test_policy_load_and_validation(tmp_path):
    policy = load_risk_policy(ROOT / "config/risk-legacy.yaml")
    assert policy.red_light_base_score == 50
    with pytest.raises(ValueError):
        RiskPolicy(warning_score=20)
    with pytest.raises(ValueError):
        RiskPolicy(duplicate_cooldown_ms=True)
    with pytest.raises(ValueError):
        RiskPolicy(distraction_threshold=1.1)
    path = tmp_path / "risk.yaml"
    path.write_text("schema_version: 1\nrisk:\n  lane_warning_offset_m: 0.45\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown risk settings"):
        load_risk_policy(path)


def test_rule_demo_scenarios_are_context_sensitive():
    results = {name: result.assessment for name, result in run_rule_scenarios()}
    assert results["normal"].stable_level is RiskLevel.NORMAL
    assert not results["normal"].degraded
    assert results["red_light_stopped"].stable_level is RiskLevel.NORMAL
    assert results["phone_use"].stable_level is RiskLevel.WARNING
    assert results["siren_stopped"].stable_level is RiskLevel.CAUTION
    assert results["siren_fast_rear"].stable_level is RiskLevel.WARNING
    assert results["multiple_risks"].selected_risk.code == "red_light_approach"


def test_backward_time_is_rejected_before_evaluation():
    engine = RiskEngine()
    engine.assess(snapshot(), now_ns=NOW)
    with pytest.raises(ValueError, match="time must not move backwards"):
        engine.assess(snapshot(), now_ns=NOW - 1)


@pytest.mark.parametrize("signal", list(TurnSignal))
@pytest.mark.parametrize("zone", ["front", "rear", "left", "right", "blind_spot"])
def test_excluded_inputs_cannot_create_a_risk(signal, zone):
    road = external(
        objects=(obj(distance_m=0.1, relative_speed_mps=100, ttc_s=0.001, zone=zone),),
        lane_offset_m=5, lane_heading_error_deg=90, lane_confidence=1,
    )
    result = assess(
        vehicle=vehicle(accelerator_pct=100, turn_signal=signal),
        external=(road,), ins=InsState(header("mock.ins"), longitudinal_accel_mps2=100),
    )
    assert result.candidates == ()
    assert result.raw_level is RiskLevel.NORMAL
    assert "acceleration_cross_check" not in result.unavailable_rules


@pytest.mark.parametrize("confidence", [0, 0.2, 1])
def test_signal_and_audio_confidence_are_not_risk_inputs(confidence):
    road = external(traffic_light=TrafficLight.RED, traffic_light_confidence=confidence)
    result = assess(external=(road,))
    assert result.selected_risk.code == "red_light_approach"
    assert result.selected_risk.score == 70
    audio = AudioEvent(header("mock.audio"), AudioKind.SIREN, confidence, 960, "mock")
    result = assess(audio=audio)
    assert result.selected_risk.code == "emergency_vehicle"
    assert result.selected_risk.score == 35


def test_excluded_objects_cannot_escalate_driver_or_signal_risk():
    road = external(traffic_light=TrafficLight.RED)
    baseline = assess(external=(road,), driver=driver(phone_detected=True))
    changed = replace(road, objects=(obj(distance_m=.1, ttc_s=.001, zone="rear"),),
                      lane_offset_m=10, lane_confidence=1)
    result = assess(external=(changed,), driver=driver(phone_detected=True))
    assert result.candidates == baseline.candidates
