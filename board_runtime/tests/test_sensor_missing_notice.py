from types import SimpleNamespace
from codrive.models import Header,DriverState,ExternalPerception,SituationSnapshot
from codrive.risk_ml.adapter_v3 import sensor_missing_fields
from codrive.v3_feature_engine import RiskPolicy
from codrive.sensor_features import GROUPS

NOW=3_000_000_000_000


def test_unseen_signal_and_face_are_not_missing_sensor_streams():
    h=Header.now('test',1,capture_time_ns=NOW)
    s=SituationSnapshot(h,driver=DriverState(h,'cabin','test',0.,False,face_detected=False),external=(ExternalPerception(h,'front','test'),))
    missing=('traffic_light',*GROUPS['driver_valid'])
    frame=SimpleNamespace(inputs={'front_valid':True},missing_fields=missing)
    assert sensor_missing_fields(frame,s,now_ns=NOW,policy=RiskPolicy())==()
    assert frame.missing_fields==missing
    assert sensor_missing_fields(frame,s,now_ns=NOW+500_000_000,policy=RiskPolicy())==missing


def test_disconnected_front_remains_missing_even_when_face_not_detected():
    h=Header.now('test',1,capture_time_ns=NOW)
    s=SituationSnapshot(h,driver=DriverState(h,'cabin','test',0.,False,face_detected=False))
    frame=SimpleNamespace(inputs={'front_valid':False},missing_fields=('traffic_light','vehicle_count',*GROUPS['driver_valid']))
    assert sensor_missing_fields(frame,s,now_ns=NOW,policy=RiskPolicy())==('traffic_light','vehicle_count')
