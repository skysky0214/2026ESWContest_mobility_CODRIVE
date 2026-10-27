from dataclasses import replace

import pytest

from codrive.guidance import HmiArbiter, HmiPolicy
from codrive.models import (
    AudioEvent,
    AudioKind,
    BoundingBox,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    Severity,
    SituationSnapshot,
    TrackedObject,
    TrafficLight,
    VehicleState,
    Weather,
)
from codrive.v3_feature_engine import RiskEngine, RiskPolicy, load_risk_policy

NOW = 1500000000000000000


def header(source="test", now=NOW, sequence=1, boot="a"):
    return Header.now(source, sequence, capture_time_ns=now, boot_id=boot)


def snapshot(
    now=NOW,
    *,
    speed=10,
    pedal=10,
    behavior="awake",
    attention=0.95,
    drowsiness=0,
    light=TrafficLight.GREEN,
    objects=(),
    audio=None,
    env=None,
    stamps=False,
):
    h = header(now=now, sequence=now)
    vehicle = VehicleState(
        h,
        speed,
        0,
        False,
        pedal,
        signal_times_ns=(("speed_mps", now), ("accelerator_pct", now)) if stamps else (),
    )
    driver = DriverState(
        h,
        "cabin",
        "test",
        attention,
        behavior == "awake",
        behavior=behavior,
        phone_detected=behavior == "phone",
        drowsiness_score=drowsiness,
    )
    external = ExternalPerception(h, "front", "test", objects=objects, traffic_light=light)
    return SituationSnapshot(
        h, vehicle=vehicle, driver=driver, external=(external,), audio=audio, environment=env
    )


def test_policy_and_excluded_options():
    policy = load_risk_policy("config/rules/final-sensors.yaml")
    assert policy.acceleration_hold_ms == 300
    with pytest.raises(TypeError):
        RiskPolicy(lane_warning_offset_m=0.4)
    with pytest.raises(ValueError):
        RiskPolicy(environment_weight_cap=0.5)


def test_objects_are_attention_without_distance_or_collision_claim():
    obj = TrackedObject(1, "pedestrian", 0.1, BoundingBox(0.4, 0.2, 0.6, 0.9))
    e = RiskEngine()
    result = e.evaluate(snapshot(objects=(obj,)), now_ns=NOW)
    assert result.code == "road_user_attention" and result.severity is Severity.INFO
    assert "pedestrian_count=1" in result.evidence
    assert "충돌" not in HmiPolicy().render(result).display_text
    altered = replace(obj, distance_m=0.001, ttc_s=0.001, zone="blind_spot", in_path=True)
    second = RiskEngine().evaluate(snapshot(objects=(altered,)), now_ns=NOW)
    assert result.score == second.score and result.severity is second.severity
    impaired = RiskEngine().evaluate(
        snapshot(objects=(obj,), behavior="distracted", attention=0.2), now_ns=NOW
    )
    assert impaired.severity is Severity.WARNING
    assert all(x.code not in {"pedestrian_collision", "lane_departure"} for x in e.last_candidates)


@pytest.mark.parametrize("kind", [AudioKind.SIREN, AudioKind.HORN])
def test_direction_changes_update_display_but_share_original_speech_cooldown(kind):
    e = RiskEngine()
    policy = HmiPolicy()
    arbiter = HmiArbiter()

    def get(direction, now):
        audio = AudioEvent(header(now=now), kind, 0.2, 960, "test", direction)
        return e.evaluate(snapshot(now, audio=audio), now_ns=now)

    left = get("left", NOW)
    assert get("left", NOW + 1000000) is None
    right = get("right", NOW + 2000000)
    assert left.score == right.score and left.severity is right.severity
    lcmd, rcmd = policy.render(left), policy.render(right)
    assert "왼쪽" in lcmd.display_text and "왼쪽" in lcmd.tts_text
    assert "오른쪽" in rcmd.display_text and "오른쪽" in rcmd.tts_text
    assert arbiter.accept(lcmd, now_ns=NOW).speak
    assert not arbiter.accept(rcmd, now_ns=NOW + 2000000).speak


def test_driver_duration_requires_new_samples_and_resets_after_gap():
    e = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
    first = snapshot(behavior="drowsy", drowsiness=0.9)
    assert e.evaluate(first, now_ns=NOW).severity is Severity.WARNING
    e.evaluate(first, now_ns=NOW + 200000000)
    assert not any(c.severity is Severity.CRITICAL for c in e.last_candidates)
    for i in range(1, 22):
        now = NOW + i * 200000000
        e.evaluate(snapshot(now, behavior="drowsy", drowsiness=0.9), now_ns=now)
    assert e.last_candidates[0].severity is Severity.CRITICAL
    now += 1000000000
    e.evaluate(snapshot(now, behavior="drowsy", drowsiness=0.9), now_ns=now)
    assert e.last_candidates[0].severity is Severity.WARNING


def test_night_and_weather_weights_capped_and_expire_independently():
    e = RiskEngine()
    env = EnvironmentState(
        header(),
        Weather.SNOW,
        True,
        10,
        100,
        0,
        True,
        snowfall_cm_h=1,
        weather_time_ns=NOW,
        weather_valid_until_ns=NOW + 1000000000,
        daylight_valid=True,
    )
    base = RiskEngine().evaluate(snapshot(light=TrafficLight.RED, speed=3), now_ns=NOW)
    weighted = e.evaluate(snapshot(light=TrafficLight.RED, speed=3, env=env), now_ns=NOW)
    assert weighted.score == pytest.approx(base.score * 2)
    assert weighted.severity is Severity.CRITICAL
    # A new wrapper timestamp does not renew the weather observation.
    now = NOW + 1100000000
    env = replace(env, header=header(now=now))
    result = RiskEngine().evaluate(
        snapshot(now, light=TrafficLight.RED, speed=3, env=env), now_ns=now
    )
    assert result.score == pytest.approx(base.score * 1.15)
    invalid = replace(env, api_valid=False, daylight_valid=False)
    result = RiskEngine().evaluate(
        snapshot(now, light=TrafficLight.RED, speed=3, env=invalid), now_ns=now
    )
    assert result.score == base.score


def test_confirmed_acceleration_and_repeated_values_do_not_fake_history():
    e = RiskEngine()
    for i in range(30):
        now = NOW + i * 20000000
        e.evaluate(snapshot(now, speed=10 + i * 0.1, stamps=True), now_ns=now)
    assert any(c.code == "rapid_acceleration" for c in e.last_candidates)
    assert all("unintended" not in c.code for c in e.last_candidates)
    # A stream without per-signal proof cannot create acceleration rules.
    e = RiskEngine()
    for i in range(30):
        now = NOW + i * 20000000
        e.evaluate(snapshot(now, speed=i * 2, stamps=False), now_ns=now)
    assert not e.last_candidates


def test_rapid_pedal_and_no_steering_or_launch_rules():
    e = RiskEngine()
    for i in range(20):
        now = NOW + i * 20000000
        e.evaluate(snapshot(now, pedal=min(100, i * 8), stamps=True), now_ns=now)
        if any(c.code == "rapid_pedal" for c in e.last_candidates):
            break
    else:
        pytest.fail("pedal step not detected")
    assert all(c.code in {"rapid_pedal", "rapid_acceleration"} for c in e.last_candidates)


def test_stale_signal_clock_and_boot_reset_history():
    e = RiskEngine()
    for i in range(5):
        now = NOW + i * 20000000
        e.evaluate(snapshot(now, speed=10 + i, stamps=True), now_ns=now)
    sample = snapshot(now + 20000000, speed=80, stamps=True)
    sample = replace(
        sample, vehicle=replace(sample.vehicle, header=replace(sample.vehicle.header, boot_id="b"))
    )
    e.evaluate(sample, now_ns=now + 20000000)
    assert not e.last_candidates
    stale = replace(
        sample, vehicle=replace(sample.vehicle, signal_times_ns=(("speed_mps", NOW - 200000000),))
    )
    e.evaluate(stale, now_ns=now + 20000000)
    assert not e.last_candidates


def test_unavailable_vehicle_never_means_moving():
    sample = snapshot(behavior="phone")
    result = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(
        replace(sample, vehicle=None), now_ns=NOW
    )
    assert result.severity is Severity.INFO and result.code == "driver_phone_observation"
