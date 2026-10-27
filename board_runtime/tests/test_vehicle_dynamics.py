from dataclasses import replace
import pytest
from codrive.models import Header, VehicleState
from codrive.vehicle_dynamics import SpeedAcceleration
from codrive.risk import RiskEngine, RiskPolicy
from codrive.fusion import SituationFusion, FusionPolicy
from codrive.guidance import HmiPolicy

START = 1_000_000_000_000

def sample(index, *, acceleration=4, pedal=90, brake=False, source='pi5.can_ecu', boot='boot-a'):
    stamp=START+index*50_000_000
    h=replace(Header.now(source,index+1,capture_time_ns=stamp),boot_id=boot)
    return VehicleState(h, speed_mps=5+acceleration*index*.05, steering_wheel_deg=0,
                        brake_pressed=brake,accelerator_pct=pedal)

def test_slope_units_and_unique_samples():
    estimate=SpeedAcceleration()
    for i in range(11):
        v=sample(i)
        estimate.observe(v,v.header.capture_time_ns)
        count=len(estimate.samples)
        estimate.observe(v,v.header.capture_time_ns+1_000_000)
        assert len(estimate.samples)==count
    assert estimate.acceleration_mps2==pytest.approx(4)
    assert estimate.status()['span_ms']==500

@pytest.mark.parametrize('kind',['missing','stale','future','invalid','source','boot','gap','clock','jump','small_jump'])
def test_untrustworthy_history_never_emits_a_stale_acceleration(kind):
    e=SpeedAcceleration()
    for i in range(11):e.observe(sample(i),START+i*50_000_000)
    v=sample(11);now=v.header.capture_time_ns
    if kind=='missing':v=None
    elif kind=='stale':now+=300_000_000
    elif kind=='future':now-=10_000_000
    elif kind=='invalid':v=replace(v,can_valid=False)
    elif kind=='source':v=sample(11,source='other')
    elif kind=='boot':v=sample(11,boot='boot-b')
    elif kind=='gap':v=sample(20);now=v.header.capture_time_ns
    elif kind=='clock':v=sample(1);now=v.header.capture_time_ns
    elif kind=='jump':v=replace(v,speed_mps=100)
    elif kind=='small_jump':v=replace(v,speed_mps=v.speed_mps+2)
    assert e.observe(v,now) is None

@pytest.mark.parametrize(('pedal','brake','acceleration','warn'),[(90,False,4,True),(69,False,4,False),(90,True,4,False),(90,False,1,False),(90,False,-4,False)])
def test_realistic_can_path_without_ins_drives_existing_warning_template(pedal,brake,acceleration,warn):
    engine=RiskEngine(policy=RiskPolicy(can_speed_acceleration_enabled=True))
    fusion=SituationFusion(policy=FusionPolicy(expected_camera_ids=(),vehicle_ttl_ms=500))
    events=[]
    for i in range(23):
        v=sample(i,pedal=pedal,brake=brake,acceleration=acceleration)
        stamp=v.header.capture_time_ns
        assert fusion.ingest(v,received_time_ns=stamp)
        snapshot=fusion.snapshot(now_ns=stamp)
        assert snapshot.ins is None
        event=engine.evaluate(snapshot,now_ns=stamp)
        if event:events.append(event)
    assert bool(events)==warn
    if warn:
        assert events[0].code=='rapid_acceleration'
        assert 'acceleration_source=carla_speed_over_can' in events[0].evidence
        command=HmiPolicy().render(events[0])
        assert command.display_text=='가속페달 떼세요'

def test_no_pedal_placeholder_is_accepted_as_measured_input():
    from codrive.hardware.stm32_can import EcuTelemetry
    with pytest.raises((ValueError,KeyError)):
        EcuTelemetry.parse('ECU,ms=1000,speed_raw=1000,brake=0,steer_raw=0')
