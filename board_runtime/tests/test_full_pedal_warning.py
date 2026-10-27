from dataclasses import replace
from codrive.models import Header,VehicleState,SituationSnapshot
from codrive.risk import RiskEngine,RiskPolicy

T=3_000_000_000_000

def frame(ms,pedal=100,brake=False,stamp=None):
    now=T+ms*1_000_000;stamp=now if stamp is None else stamp
    h=Header.now('pi5.eq900_obd',ms+1,capture_time_ns=now)
    v=VehicleState(h,0.,0.,brake,pedal,signal_times_ns=(('accelerator_pct',stamp),('brake_pressed',now)))
    return SituationSnapshot(h,vehicle=v)

def evaluate(engine,ms,**kwargs):
    s=frame(ms,**kwargs);return engine.evaluate(s,now_ns=s.header.capture_time_ns)

def test_full_pedal_warns_with_no_ins_and_zero_speed():
    e=RiskEngine(policy=RiskPolicy(full_pedal_warning_enabled=True),vehicle_ttl_ms=750)
    assert evaluate(e,0) is None
    assert evaluate(e,300).code=='full_accelerator'
    assert e.assessment.stable_level.value=='WARNING'
    assert 'acceleration_not_measured' in e.assessment.selected_risk.evidence

def test_normal_simulation_mode_does_not_enable_pedal_only_warning():
    e=RiskEngine(vehicle_ttl_ms=750)
    evaluate(e,0);assert evaluate(e,350) is None

def test_stale_or_repeated_pedal_does_not_advance_confirmation():
    e=RiskEngine(policy=RiskPolicy(full_pedal_warning_enabled=True),vehicle_ttl_ms=750)
    evaluate(e,0)
    for ms in [100,300,500,900]:assert evaluate(e,ms,stamp=T) is None

def test_release_brake_and_subthreshold_reset_condition():
    for kwargs in [dict(brake=True),dict(pedal=89.9),dict(pedal=0)]:
        e=RiskEngine(policy=RiskPolicy(full_pedal_warning_enabled=True),vehicle_ttl_ms=750)
        evaluate(e,0);assert evaluate(e,300,**kwargs) is None
        assert evaluate(e,400) is None
        assert evaluate(e,700).code=='full_accelerator'
