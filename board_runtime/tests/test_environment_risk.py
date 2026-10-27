from dataclasses import replace
import pytest
from codrive.models import (Header, EnvironmentState, Weather, BoundingBox, TrackedObject,
                           ExternalPerception, SituationSnapshot, VehicleState, TrafficLight)
from codrive.risk import RiskEngine
from codrive.team_core.risk.policy import RiskPolicy
from codrive.environment_risk import environment_context

NOW = 3_000_000_000_000
H = Header.now('test', 1, capture_time_ns=NOW)


def environment(**kwargs):
    values = dict(header=H, weather=Weather.CLEAR, is_night=False, api_valid=True,
                  daylight_valid=True, weather_time_ns=NOW, weather_valid_until_ns=NOW+3600_000_000_000)
    values.update(kwargs)
    return EnvironmentState(**values)


def snapshot(env, *, ttc=4, in_path=True, relative_speed=None, signal=TrafficLight.UNKNOWN):
    obj = TrackedObject(1, 'car', .95, BoundingBox(.4,.2,.6,.8), distance_m=10,
                        ttc_s=ttc, in_path=in_path, relative_speed_mps=relative_speed)
    frame = ExternalPerception(H, 'front', 'test', objects=(obj,),
                               traffic_light=signal, traffic_light_confidence=.99)
    return SituationSnapshot(H, vehicle=VehicleState(H, 10, 0, False, 20),
                             external=(frame,), environment=env)


@pytest.mark.parametrize('env,expected', [
    (environment(), 1),
    (environment(weather=Weather.RAIN), 1.2),
    (environment(is_night=True), 1.15),
    (environment(weather=Weather.RAIN,is_night=True), 1.38),
    (environment(weather=Weather.SNOW), 1.35),
    (environment(weather=Weather.FOG), 1.4),
    (environment(weather=Weather.THUNDER), 1.35),
    (environment(weather=Weather.SNOW,visibility_m=100), 2),
])
def test_weather_and_solar_weight(env, expected):
    weight, _ = environment_context(env, now_ns=NOW, policy=RiskPolicy())
    assert weight == pytest.approx(expected)


def test_weather_expiry_does_not_disable_valid_solar_context():
    env = environment(weather=Weather.RAIN,is_night=True,weather_valid_until_ns=NOW)
    assert environment_context(env, now_ns=NOW, policy=RiskPolicy())[0] == 1.15
    assert environment_context(replace(env, daylight_valid=False),now_ns=NOW,policy=RiskPolicy())[0] == 1
    assert environment_context(environment(weather=Weather.RAIN,api_valid=False),now_ns=NOW,policy=RiskPolicy())[0] == 1


@pytest.mark.parametrize('age',[3001,-1])
def test_stale_or_future_republished_context_is_neutral(age):
    env=environment(header=replace(H,capture_time_ns=NOW-age*1_000_000),weather=Weather.RAIN,is_night=True)
    assert environment_context(env,now_ns=NOW,policy=RiskPolicy())[0] == 1


def test_rain_increases_actual_team_candidate_before_fsm():
    clear, rain = RiskEngine(), RiskEngine()
    clear.evaluate(snapshot(environment()),now_ns=NOW)
    event = rain.evaluate(snapshot(environment(weather=Weather.RAIN)),now_ns=NOW)
    assert clear.assessment.stable_level.value == 'CAUTION'
    assert rain.assessment.stable_level.value == 'WARNING'
    assert event.score == pytest.approx(clear.assessment.selected_risk.score*1.2)
    assert 'environment_weight=1.20' in event.evidence
    assert 'weather=rain' in event.evidence


def test_rain_night_updates_signal_and_lane_scores():
    clear, wet = RiskEngine(team_policy=RiskPolicy(lane_confirm_ms=0)), RiskEngine(team_policy=RiskPolicy(lane_confirm_ms=0))
    a=snapshot(environment(), signal=TrafficLight.RED)
    b=snapshot(environment(weather=Weather.RAIN,is_night=True), signal=TrafficLight.RED)
    a=replace(a,external=(replace(a.external[0],lane_offset_m=.7,lane_confidence=.9),))
    b=replace(b,external=(replace(b.external[0],lane_offset_m=.7,lane_confidence=.9),))
    clear.evaluate(a,now_ns=NOW);wet.evaluate(b,now_ns=NOW)
    old={c.code:c.score for c in clear.assessment.candidates}
    new={c.code:c.score for c in wet.assessment.candidates}
    for code in ('red_light_approach','lane_departure'):
        assert new[code] == pytest.approx(old[code]*1.38)


def test_ttc_emergency_and_fsm_release_remain_intact():
    e=RiskEngine()
    r=e.evaluate(snapshot(environment(weather=Weather.RAIN,is_night=True),ttc=1.2),now_ns=NOW)
    assert r.code == 'object_collision' and r.message_key == 'object_brake'
    assert e.assessment.stable_level.value == 'EMERGENCY'
    e.evaluate(SituationSnapshot(H),now_ns=NOW+100_000_000)
    assert e.assessment.held
    e.evaluate(SituationSnapshot(H),now_ns=NOW+600_000_000)
    assert e.assessment.stable_level.value == 'NORMAL'


@pytest.mark.parametrize('in_path,relative_speed',[(False,None),(True,-1)])
def test_adverse_weather_preserves_object_safety_caps(in_path,relative_speed):
    e=RiskEngine()
    e.evaluate(snapshot(environment(weather=Weather.SNOW,visibility_m=100),ttc=1.2,
                        in_path=in_path,relative_speed=relative_speed),now_ns=NOW)
    assert e.assessment.stable_level.value != 'EMERGENCY'


def test_weather_alone_never_creates_an_alert():
    e=RiskEngine()
    assert e.evaluate(SituationSnapshot(H,environment=environment(weather=Weather.RAIN,is_night=True)),now_ns=NOW) is None
