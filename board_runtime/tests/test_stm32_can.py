import pytest
from codrive.hardware.stm32_can import EcuTelemetry
from codrive.models import Header

BASE = dict(ms=1000,speed_raw=1234,brake=1,steer_raw=-456,rx=3,valid=3,speed_n=1,brake_n=1,steer_n=1,speed_ms=980,brake_ms=981,steer_ms=982,err=0,invalid=0,lost=0,bo=0,lec=0)
def telemetry(**changes):
    values=BASE | changes
    return EcuTelemetry.parse('ECU,'+','.join(f'{k}={v}' for k,v in values.items()))
def test_actual_ecu_units_and_metadata():
    h=Header.now('pi5.can_ecu',1)
    v=telemetry().to_vehicle_state(h)
    assert v.header==h and v.speed_mps==pytest.approx(12.34/3.6)
    assert v.steering_wheel_deg==pytest.approx(-45.6) and v.brake_pressed and v.can_valid
    assert v.gear=='unknown'
def test_missing_any_id_never_reports_can_valid():
    for key in ('speed_n','brake_n','steer_n'):
        assert not telemetry(**{key:0}).valid()
def test_stale_or_bus_off_is_invalid():
    assert not telemetry(ms=1600).valid()
    assert not telemetry(bo=1).valid()
def test_latched_historical_error_does_not_hide_fresh_received_frames():
    assert telemetry(lec=3).valid()
    assert not telemetry(lec=3,ms=1600).valid()
def test_uint32_uptime_wrap():
    assert telemetry(ms=20,speed_ms=0xfffffff0,brake_ms=0xfffffff0,steer_ms=0xfffffff0).valid()
def test_reject_wrong_or_malformed_input():
    for s in ('PC decode: speed=12.34','ECU,ms=12','ECU,ms=12,ms=13'):
        with pytest.raises((ValueError,KeyError)):EcuTelemetry.parse(s)
@pytest.mark.parametrize('changes',[dict(brake=2),dict(speed_raw=-1),dict(steer_raw=4501),dict(rx=-1),dict(ms=0x100000000)])
def test_reject_out_of_range(changes):
    with pytest.raises(ValueError):telemetry(**changes)


def test_invalid_can_report_clears_fused_vehicle_immediately():
    from codrive.fusion import SituationFusion, FusionPolicy
    fusion=SituationFusion(policy=FusionPolicy(vehicle_ttl_ms=500))
    h=Header.now('pi5.can_ecu',1)
    assert fusion.ingest(telemetry(bo=1).to_vehicle_state(h))
    assert fusion.snapshot().vehicle is None


def test_ecu_signal_times_reach_v3_without_inventing_pedal():
    from codrive.fusion import SituationFusion, FusionPolicy
    from codrive.v3_feature_engine import RiskEngine, RiskPolicy
    now = 10_000_000_000
    h = Header.now('pi5.can_ecu', 1, capture_time_ns=now)
    vehicle = telemetry().to_vehicle_state(h)
    assert dict(vehicle.signal_times_ns) == {
        'speed_mps': now - 20_000_000,
        'brake_pressed': now - 19_000_000,
        'steering_wheel_deg': now - 18_000_000,
    }
    fusion = SituationFusion(policy=FusionPolicy(vehicle_ttl_ms=500))
    assert fusion.ingest(vehicle, received_time_ns=now)
    engine = RiskEngine(policy=RiskPolicy(vehicle_ttl_ms=500))
    engine.evaluate(fusion.snapshot(now_ns=now), now_ns=now)
    features = engine.last_features
    assert features['speed_valid'] and features['brake_valid']
    assert features['speed_mps'] == pytest.approx(12.34 / 3.6)
    assert not features['pedal_valid'] and features['accelerator_pct'] is None
    engine.evaluate(fusion.snapshot(now_ns=now + 501_000_000), now_ns=now + 501_000_000)
    assert not engine.last_features['speed_valid']


def test_v2_actual_pedal_timestamp_and_units():
    sample = telemetry(v=2, accel_raw=3456, accel_n=1, accel_ms=985)
    h = Header.now('pi5.can_ecu', 1)
    vehicle = sample.to_vehicle_state(h)
    assert vehicle.accelerator_pct == pytest.approx(34.56)
    assert dict(vehicle.signal_times_ns)['accelerator_pct'] == h.capture_time_ns - 15_000_000
    assert not telemetry(v=2, accel_raw=0, accel_n=0, accel_ms=985).valid()
