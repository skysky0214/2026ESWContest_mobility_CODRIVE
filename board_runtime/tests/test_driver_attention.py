"""Duration gating is shared by risk, HMI status and the actual speech path."""
from dataclasses import replace

import pytest

from codrive.driver_attention import DriverAttentionGate
from codrive.guidance import HmiArbiter, HmiPolicy
from codrive.models import DriverState, Header, SituationSnapshot, VehicleState
from codrive.observation_speech import ObservationSpeechGate
from codrive.risk import RiskEngine, RiskPolicy

START = 5_000_000_000_000
MS = 1_000_000


def driver(offset, behavior="awake", *, boot="one", face=True):
    return DriverState(Header.now("jetson-cabin.dms", offset + 1,
        capture_time_ns=START + offset * MS, boot_id=boot), "cabin", "fixture",
        .9 if behavior == "awake" else .2, behavior == "awake",
        behavior=behavior, face_detected=face)


def advance(gate, start, end, behavior="awake", **kwargs):
    for offset in range(start, end + 1, 100):
        gate.update(driver(offset, behavior, **kwargs), now_ns=START + offset * MS)


@pytest.mark.parametrize("behavior,threshold", [("drowsy", 2000), ("head_drop", 2000), ("distracted", 3000)])
def test_continuous_fresh_behavior_confirms_only_at_threshold(behavior, threshold):
    gate = DriverAttentionGate()
    advance(gate, 0, 500)
    advance(gate, 600, 600 + threshold - 100, behavior)
    assert gate.active_behavior is None
    assert gate.status()["display_behavior"] == "awake"
    advance(gate, 600 + threshold, 600 + threshold, behavior)
    assert gate.active_behavior == ("drowsy" if behavior == "head_drop" else behavior)


def test_blinks_and_brief_glances_do_not_accumulate_between_normal_frames():
    gate = DriverAttentionGate()
    advance(gate, 0, 500)
    for start in range(600, 12000, 600):
        advance(gate, start, start + 200, "drowsy")
        advance(gate, start + 300, start + 500)
        assert gate.active_behavior is None
        assert gate.status()["display_behavior"] == "awake"
    advance(gate, 12000, 14500, "distracted")
    assert gate.active_behavior is None


def test_recovery_requires_one_second_but_no_face_and_missing_are_unknown_immediately():
    gate = DriverAttentionGate()
    advance(gate, 0, 2000, "drowsy")
    advance(gate, 2100, 3000)
    assert gate.active_behavior == "drowsy"
    advance(gate, 3100, 3100)
    assert gate.active_behavior is None
    advance(gate, 3200, 5200, "drowsy")
    advance(gate, 5300, 5300, "awake", face=False)
    assert gate.status()["status"] == "unknown"
    assert gate.status()["display_behavior"] is None
    gate.update(None, now_ns=START + 5400 * MS)
    assert gate.active_behavior is None


def test_cached_frames_restarts_time_reversal_and_frame_gaps_cannot_confirm():
    gate = DriverAttentionGate()
    sample = driver(0, "drowsy")
    for offset in range(0, 2100, 100):
        gate.update(sample, now_ns=START + offset * MS)
    assert gate.active_behavior is None and gate.status()["status"] == "unknown"
    advance(gate, 2200, 3700, "drowsy")
    advance(gate, 3800, 4900, "drowsy", boot="restarted")
    assert gate.active_behavior is None
    advance(gate, 5400, 6500, "drowsy", boot="restarted")
    assert gate.active_behavior is None
    gate.update(driver(6000, "drowsy", boot="restarted"), now_ns=START + 6000 * MS)
    assert gate.active_behavior is None


@pytest.mark.parametrize("behavior,threshold", [("drowsy", 2000), ("distracted", 3000)])
@pytest.mark.parametrize("moving", [False, True])
def test_warning_and_tts_cannot_bypass_confirmation_at_any_vehicle_speed(behavior, threshold, moving):
    engine = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
    speech, arbiter = ObservationSpeechGate(), HmiArbiter(repeat_speech_gap_ms=10000)
    events, spoken = [], []
    for offset in range(0, threshold + 401, 100):
        now = START + offset * MS
        sample = driver(offset, behavior)
        snapshot = SituationSnapshot(Header.now("pi5.fusion", offset + 1, capture_time_ns=now),
            driver=sample, vehicle=VehicleState(Header.now("pi5.can_ecu", offset + 1,
                capture_time_ns=now), speed_mps=5 if moving else 0,
                steering_wheel_deg=0, brake_pressed=False, accelerator_pct=0))
        event = engine.evaluate(snapshot, now_ns=now)
        speech.update(sample, now_ns=now)
        if event is not None:
            events.append(offset)
            command = HmiPolicy().render(event)
            decision = arbiter.accept(command, now_ns=now, allow_speech=speech.eligible(command, now_ns=now))
            if decision and decision.speak:
                spoken.append(offset)
                speech.mark_spoken(command)
    assert events[0] == threshold
    assert spoken == [threshold]


def test_phone_is_not_delayed_and_future_frames_are_unknown():
    now = START
    snapshot = SituationSnapshot(Header.now("pi5.fusion", 1, capture_time_ns=now), driver=driver(0, "phone"))
    event = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(snapshot, now_ns=now)
    assert event.code == "driver_phone_observation"
    gate = DriverAttentionGate()
    gate.update(driver(0, "drowsy"), now_ns=now)
    future = replace(driver(100, "drowsy"), header=replace(driver(100).header, capture_time_ns=now + 1))
    gate.update(future, now_ns=now)
    assert gate.status()["status"] == "unknown"


def test_transport_latency_does_not_add_another_speech_confirmation_delay():
    engine = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
    speech = ObservationSpeechGate()
    for offset in range(0, 2001, 100):
        sample = driver(offset, "drowsy")
        now = sample.header.capture_time_ns + (150 - offset // 20) * MS
        snapshot = SituationSnapshot(Header.now("pi5.fusion", offset + 1, capture_time_ns=now), driver=sample)
        event = engine.evaluate(snapshot, now_ns=now)
        speech.update(sample, now_ns=now)
        if event is not None:
            assert offset == 2000
            assert speech.eligible(HmiPolicy().render(event), now_ns=now)
