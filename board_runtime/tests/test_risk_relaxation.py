from dataclasses import replace
import pytest
from codrive.models import Header,VehicleState,ExternalPerception,SituationSnapshot,TurnSignal
from codrive.risk import RiskEngine,RiskPolicy
from codrive.team_core.risk.policy import RiskPolicy as TeamPolicy

START=3_000_000_000_000
MS=1_000_000


def sample(i,*,pedal=90,acceleration=4,brake=False):
    now=START+i*50*MS
    h=Header.now('pi5.can_ecu',i+1,capture_time_ns=now)
    return VehicleState(h,5+acceleration*i*.05,0,brake,pedal)


@pytest.mark.parametrize('pedal,acceleration,brake,warn',[(90,4,False,True),(89.9,4,False,False),(90,3.99,False,False),(90,4,True,False)])
def test_acceleration_requires_both_new_limits_and_confirmation(pedal,acceleration,brake,warn):
    e=RiskEngine(policy=RiskPolicy(can_speed_acceleration_enabled=True))
    first=None
    for i in range(23):
        v=sample(i,pedal=pedal,acceleration=acceleration,brake=brake)
        event=e.evaluate(SituationSnapshot(v.header,vehicle=v),now_ns=v.header.capture_time_ns)
        if event and first is None:first=v.header.capture_time_ns
    assert (first is not None)==warn
    if warn:assert first>=START+800*MS


def lane_frame(i,offset=.65,signal=TurnSignal.OFF,confidence=.9):
    now=START+i*100*MS;h=Header.now('test',i+1,capture_time_ns=now)
    v=VehicleState(h,10,0,False,0,turn_signal=signal)
    p=ExternalPerception(h,'front','test',lane_offset_m=offset,lane_confidence=confidence)
    return SituationSnapshot(h,vehicle=v,external=(p,))


@pytest.mark.parametrize('offset,level',[(.54,'NORMAL'),(.55,'WARNING'),(.924,'WARNING'),(.925,'EMERGENCY')])
def test_lane_threshold_and_capture_duration(offset,level):
    e=RiskEngine()
    for i in range(4):
        s=lane_frame(i,offset);event=e.evaluate(s,now_ns=s.header.capture_time_ns)
        if i<3:assert event is None
    assert e.assessment.stable_level.value==level


def test_invalid_lane_or_indicator_restarts_confirmation():
    for invalid in [lane_frame(3,None),lane_frame(3,.7,TurnSignal.LEFT)]:
        e=RiskEngine()
        for i in range(3):
            s=lane_frame(i,.7);e.evaluate(s,now_ns=s.header.capture_time_ns)
        assert e.evaluate(invalid,now_ns=invalid.header.capture_time_ns) is None
        for i in range(4,8):
            s=lane_frame(i,.7);event=e.evaluate(s,now_ns=s.header.capture_time_ns)
            if i<7:assert event is None
        assert event is not None


def test_repeated_capture_cannot_advance_lane_confirmation():
    e=RiskEngine();s=lane_frame(0,.7)
    for offset in [0,50,100,150,200]:
        assert e.evaluate(s,now_ns=START+offset*MS) is None
