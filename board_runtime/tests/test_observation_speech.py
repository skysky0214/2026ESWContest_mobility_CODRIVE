from dataclasses import replace

import pytest

from codrive.guidance import HmiArbiter, HmiPolicy
from codrive.models import DriverState, Header, RiskEvent, Severity
from codrive.observation_speech import ObservationSpeechGate

START = 5_000_000_000_000
MS = 1_000_000


def command(now, *, severity=Severity.INFO, code="driver_attention_observation"):
    return HmiPolicy().render(RiskEvent(
        Header.now("test", 1, capture_time_ns=now), str(now), code, severity, 105,
        "observe_driver", "attention_observation", (), now + 1_500 * MS,
    ))


def driver(now, kind="attention"):
    return DriverState(
        Header.now("jetson-cabin.dms", 1, capture_time_ns=now), "cabin", "fixture",
        .9 if kind == "normal" else .2, kind == "normal", face_detected=kind != "missing",
        phone_detected=kind == "phone",
    )


def observe(gate, start_ms, end_ms, kind="attention"):
    for offset in range(start_ms, end_ms + 1, 100):
        now = START + offset * MS
        gate.update(driver(now, kind), now_ns=now)


def test_short_detection_does_not_consume_cooldown_and_sustained_condition_repeats_every_ten_seconds():
    gate, arbiter = ObservationSpeechGate(), HmiArbiter(repeat_speech_gap_ms=10_000)
    spoken = []
    for offset in range(0, 30_001, 100):
        now = START + offset * MS
        gate.update(driver(now), now_ns=now)
        cmd = command(now)
        decision = arbiter.accept(cmd, now_ns=now, allow_speech=gate.eligible(cmd, now_ns=now))
        if decision.speak:
            gate.mark_spoken(cmd)
            spoken.append(offset)
    assert spoken == [2000, 12000, 22000]


def test_brief_detection_and_no_face_input_never_qualify_as_confirmation():
    gate = ObservationSpeechGate()
    observe(gate, 0, 1500)
    observe(gate, 1600, 2000, "missing")
    observe(gate, 2100, 3500)
    now = START + 3500 * MS
    assert not gate.eligible(command(now), now_ns=now)


def test_missing_input_cannot_trigger_speech_but_fresh_reconfirmed_input_needs_no_awake():
    gate = ObservationSpeechGate()
    observe(gate, 0, 2000)
    gate.mark_spoken(command(START + 2000 * MS))
    observe(gate, 2100, 9000, "missing")
    now = START + 9000 * MS
    assert not gate.eligible(command(now), now_ns=now)
    observe(gate, 9100, 11000)
    now = START + 11000 * MS
    assert not gate.eligible(command(now), now_ns=now)
    observe(gate, 11100, 12000)
    now = START + 12000 * MS
    assert gate.eligible(command(now), now_ns=now)


def test_awake_input_does_not_bypass_the_repeat_interval():
    gate = ObservationSpeechGate()
    observe(gate, 0, 2000)
    gate.mark_spoken(command(START + 2000 * MS))
    observe(gate, 2100, 7200, "normal")
    observe(gate, 7300, 9300)
    now = START + 9300 * MS
    assert not gate.eligible(command(now), now_ns=now)


def test_stale_driver_and_tick_gap_restart_confirmation():
    gate = ObservationSpeechGate()
    gate.update(driver(START), now_ns=START)
    now = START + 2500 * MS
    gate.update(driver(START), now_ns=now)
    assert not gate.eligible(command(now), now_ns=now)
    gate.update(driver(now), now_ns=now)
    assert not gate.eligible(command(now), now_ns=now)


@pytest.mark.parametrize("severity", [Severity.WARNING, Severity.CRITICAL])
def test_warning_and_critical_priority_are_not_delayed(severity):
    gate, arbiter = ObservationSpeechGate(), HmiArbiter()
    cmd = command(START, severity=severity, code="driver_inattention")
    assert gate.eligible(cmd, now_ns=START)
    assert arbiter.accept(cmd, now_ns=START, allow_speech=gate.eligible(cmd, now_ns=START)).speak


def test_changing_observation_code_does_not_allow_a_previous_code_to_repeat():
    gate = ObservationSpeechGate()
    observe(gate, 0, 2000)
    gate.mark_spoken(command(START + 2000 * MS))
    observe(gate, 2100, 4500, "phone")
    phone = replace(command(START + 4500 * MS), risk_code="driver_phone_observation")
    assert gate.eligible(phone, now_ns=START + 4500 * MS)
    gate.mark_spoken(phone)
    observe(gate, 4600, 7000)
    assert not gate.eligible(command(START + 7000 * MS), now_ns=START + 7000 * MS)
