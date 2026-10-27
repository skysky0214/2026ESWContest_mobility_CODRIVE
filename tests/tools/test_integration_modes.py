"""Mode planning is testable without contacting or restarting any board."""
from pathlib import Path
import subprocess
import pytest

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/select-integration-mode.sh'

@pytest.mark.parametrize('mode,profiles',[
 ('carla',('pi-carla-demo.conf','jetson1-carla-demo.conf')),
 ('carla-screen',('pi-carla-screen.conf','jetson1-carla-screen.conf')),
 ('real-vehicle',('pi-real-vehicle.conf','jetson1-real-vehicle.conf')),
])
def test_three_mode_plans_are_explicit_and_need_no_boards(mode,profiles):
    result=subprocess.run([str(SCRIPT),mode,'--dry-run'],text=True,capture_output=True,timeout=3)
    assert result.returncode==0,result.stderr
    assert all(name in result.stdout for name in profiles)


def test_old_screen_geometry_requires_explicit_opt_in():
    result=subprocess.run([str(SCRIPT),'carla-screen','--screen-calibrated','--dry-run'],text=True,capture_output=True,timeout=3)
    assert result.returncode==0
    assert 'pi-carla-screen-calibrated.conf' in result.stdout
    assert 'camera/monitor must match' in result.stdout
    for mode in ['carla','real-vehicle']:
        assert subprocess.run([str(SCRIPT),mode,'--screen-calibrated','--dry-run'],capture_output=True).returncode==2


def test_unknown_mode_is_rejected_without_side_effects():
    assert subprocess.run([str(SCRIPT),'typo','--dry-run'],capture_output=True).returncode==2
