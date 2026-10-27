import importlib.util
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('feedback',Path('tools/g29_ff.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_speed_response_is_slew_limited_and_stale_status_returns_to_low_force(monkeypatch):
    a=module.AutoCenter();a.fd=1;a.strength=8;a.last_update=10
    a.dynamic={'speed_curve_kmh':[[0,8],[10,12],[30,21],[60,30],[100,36]],'slew_percent_s':8}
    clock=[10.1];writes=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0]);a._write=lambda code,value:writes.append(value)
    a.update_speed(100/3.6)
    assert a.strength==pytest.approx(8.8)
    for i in range(40):
        clock[0]+=.1;a.update_speed(100/3.6)
    assert a.strength==pytest.approx(36)
    clock[0]+=.1;a.update_speed(100/3.6,fresh=False)
    assert a.strength==pytest.approx(35.2)
    assert max(writes)<=round(65535*.4)
