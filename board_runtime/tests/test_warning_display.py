from dataclasses import replace
from codrive.guidance import HmiPolicy
from codrive.models import Header, RiskEvent, Severity
from codrive.warning_display import WarningDisplay

NOW=3_000_000_000_000
MS=1_000_000


def command(name='driver_inattention', severity=Severity.WARNING):
    return HmiPolicy().render(RiskEvent(Header.now('test',1,capture_time_ns=NOW),name,
        name,severity,65,'look_forward','look_forward',(),NOW+1500*MS))


def test_speech_keeps_matching_alert_past_risk_expiry_then_releases():
    display=WarningDisplay()
    c=command()
    assert display.update(c,None,now_ns=NOW).event_id == c.event_id
    display.update(c,c,now_ns=NOW+100*MS)
    shown=display.update(None,c,now_ns=NOW+4500*MS)
    assert shown.event_id == c.event_id and shown.expires_at_ns > NOW+4500*MS
    assert display.update(None,None,now_ns=NOW+4600*MS) is None


def test_more_urgent_warning_preempts_and_old_speech_cannot_restore_itself():
    display=WarningDisplay()
    low=command()
    high=command('pedestrian_collision',Severity.CRITICAL)
    display.update(low,low,now_ns=NOW)
    assert display.update(high,low,now_ns=NOW+100*MS).event_id == high.event_id
    assert display.update(None,low,now_ns=NOW+1800*MS).event_id == high.event_id
    assert display.update(None,low,now_ns=NOW+2200*MS) is None


def test_equal_severity_waits_for_current_speech_to_finish():
    display=WarningDisplay()
    first,second=command(),command('lane_departure')
    display.update(first,first,now_ns=NOW)
    assert display.update(second,first,now_ns=NOW+100*MS).event_id == first.event_id
    second=replace(second,expires_at_ns=NOW+5_000*MS)
    assert display.update(second,None,now_ns=NOW+4000*MS).event_id == second.event_id


def test_silent_warning_has_minimum_duration_but_new_urgent_warning_is_immediate():
    display=WarningDisplay()
    c=command()
    display.update(c,None,now_ns=NOW)
    assert display.update(None,None,now_ns=NOW+1800*MS).event_id == c.event_id
    assert display.update(None,None,now_ns=NOW+2000*MS) is None
    display.update(c,None,now_ns=NOW+2100*MS)
    high=replace(command('pedestrian_collision',Severity.CRITICAL),expires_at_ns=NOW+6000*MS)
    assert display.update(high,None,now_ns=NOW+2200*MS).event_id == high.event_id


def test_stuck_audio_worker_cannot_pin_an_alert_forever():
    display=WarningDisplay()
    c=command()
    display.update(c,c,now_ns=NOW)
    assert display.update(None,c,now_ns=NOW+14999*MS)
    assert display.update(None,c,now_ns=NOW+15000*MS) is None
