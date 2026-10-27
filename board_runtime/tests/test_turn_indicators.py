from dataclasses import replace
from codrive.models import Header, TurnSignal, TurnIndicatorState, VehicleState, to_json, from_json
from codrive.turn_indicators import IndicatorSwitch
from codrive.fusion import SituationFusion, FusionPolicy

NOW=3_000_000_000_000


def header(source, sequence=1, now=NOW):
    return Header.now(source,sequence,capture_time_ns=now)


def test_toggle_edges_and_direction_switch_preserve_other_buttons():
    s=IndicatorSwitch()
    assert s.update(5,True) is TurnSignal.LEFT
    assert s.update(5,True) is TurnSignal.LEFT  # Held button does not flicker.
    s.update(5,False)
    assert s.update(4,True) is TurnSignal.RIGHT
    s.update(4,False)
    assert s.update(4,True) is TurnSignal.OFF
    assert s.update(0,True) is TurnSignal.OFF  # Existing reverse binding untouched.
    s.update(5,True)
    s.reset()
    assert s.state is TurnSignal.OFF


def test_intent_roundtrip_and_overlay_does_not_invent_can_or_change_measurements():
    f=SituationFusion(policy=FusionPolicy(vehicle_ttl_ms=500))
    intent=TurnIndicatorState(header('laptop.g29.indicators'),TurnSignal.LEFT,True)
    assert from_json(TurnIndicatorState,to_json(intent)) == intent
    assert f.ingest(intent,received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW).vehicle is None
    v=VehicleState(header('pi5.can_ecu'),10,5,False,20,
                   signal_times_ns=(('speed_mps',NOW),))
    f.ingest(v,received_time_ns=NOW)
    actual=f.snapshot(now_ns=NOW).vehicle
    assert actual.turn_signal is TurnSignal.LEFT
    assert actual.speed_mps==10 and actual.can_valid and actual.header==v.header
    assert ('speed_mps',NOW) in actual.signal_times_ns


def test_stale_or_disconnected_indicator_cannot_keep_lane_warning_suppressed():
    f=SituationFusion(policy=FusionPolicy(vehicle_ttl_ms=1000))
    f.ingest(VehicleState(header('pi5.can_ecu'),10,0,False,20),received_time_ns=NOW)
    h=header('laptop.g29.indicators')
    f.ingest(TurnIndicatorState(h,TurnSignal.LEFT,True),received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW+501_000_000).vehicle.turn_signal is TurnSignal.OFF
    f.ingest(TurnIndicatorState(replace(h,sequence=2),TurnSignal.RIGHT,False),received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW).vehicle.turn_signal is TurnSignal.OFF


def test_indicator_source_allowlist():
    f=SituationFusion(policy=FusionPolicy(source_allowlist=(('turn_indicator','laptop.g29.indicators'),)))
    assert not f.ingest(TurnIndicatorState(header('unapproved'),TurnSignal.LEFT,True),received_time_ns=NOW)


def test_indicator_suppresses_lane_candidate_but_never_ttc_collision():
    from codrive.models import ExternalPerception, TrackedObject, BoundingBox
    from codrive.risk import RiskEngine
    f=SituationFusion()
    f.ingest(VehicleState(header('pi5.can_ecu'),10,0,False,20),received_time_ns=NOW)
    frame=ExternalPerception(header('jetson-front.front'),'front','test',lane_offset_m=.9,lane_confidence=.9)
    f.ingest(frame,received_time_ns=NOW)
    from codrive.team_core.risk.policy import RiskPolicy as TeamPolicy
    assert RiskEngine(team_policy=TeamPolicy(lane_confirm_ms=0)).evaluate(f.snapshot(now_ns=NOW),now_ns=NOW).code=='lane_departure'
    f.ingest(TurnIndicatorState(header('laptop.g29.indicators'),TurnSignal.LEFT,True),received_time_ns=NOW)
    assert RiskEngine().evaluate(f.snapshot(now_ns=NOW),now_ns=NOW) is None
    obj=TrackedObject(1,'person',.95,BoundingBox(.4,.2,.6,.8),distance_m=10,ttc_s=1.2,in_path=True)
    frame=replace(frame,header=replace(frame.header,sequence=2),objects=(obj,))
    f.ingest(frame,received_time_ns=NOW)
    assert RiskEngine().evaluate(f.snapshot(now_ns=NOW),now_ns=NOW).code=='pedestrian_collision'
