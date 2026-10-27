from types import SimpleNamespace
import pytest
from codrive.fusion import SituationFusion,FusionPolicy
from codrive.models import Header,ScenarioContext
from codrive.hardware.socketcan import SocketCanReceiver


def test_real_mode_rejects_demo_presets():
    h=Header.now('laptop.carla.scenario',1)
    f=SituationFusion(policy=FusionPolicy(allow_scenario_context=False))
    assert not f.ingest(ScenarioContext(h,'emergency-yellow'),received_time_ns=h.capture_time_ns)


def test_can_receiver_refuses_active_interface_before_opening_socket(monkeypatch):
    import codrive.hardware.socketcan as module
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='[{"linkinfo":{"info_data":{"ctrlmode":[]}}}]'))
    def fail(*a,**k):pytest.fail('Must not open an active CAN interface')
    monkeypatch.setattr(module.socket,'socket',fail)
    with pytest.raises(RuntimeError,match='listen-only'):SocketCanReceiver('can0')
