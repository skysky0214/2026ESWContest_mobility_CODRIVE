import importlib.util
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import carla_saved_scenario as scenes


def test_catalog_preserves_existing_and_adds_two_replayable_scenes():
    catalog=scenes.catalog(ROOT/'config/carla/scenarios')
    assert {'pedestrian-lane','rapid-acceleration','rain-night-slow-vehicle','emergency-yellow'} <= catalog.keys()
    for ident in ('rain-night-slow-vehicle','emergency-yellow'):
        config=json.loads(catalog[ident]['path'].read_text())
        assert config['recommended_speed_kmh']==40
        assert scenes.validate(config) is config


def test_yellow_trigger_requires_approach_same_lane_and_valid_distance():
    c={'signal_trigger_distance_m':30,'minimum_speed_kmh':5}
    row={'front_gap_to_crossing_m':29,'ego_speed_kmh':30,'ego_route_right_m':0}
    assert scenes.signal_trigger(c,row)
    for changes in ({'front_gap_to_crossing_m':31},{'front_gap_to_crossing_m':-1},
                    {'ego_speed_kmh':0},{'ego_route_right_m':3.5}):
        assert not scenes.signal_trigger(c,{**row,**changes})


def test_scene_switch_stays_locked_while_driving_or_pedal_held():
    from types import SimpleNamespace as N
    s=scenes.Scene.__new__(scenes.Scene)
    speed=[1.];throttle=[0.]
    s.ego=N(get_velocity=lambda:N(length=lambda:speed[0]),get_control=lambda:N(throttle=throttle[0]))
    s.wheel_state=lambda:{'throttle':0}
    assert not s.reset_allowed()[0]
    speed[0]=0;throttle[0]=.2
    assert not s.reset_allowed()[0]
    throttle[0]=0
    assert s.reset_allowed()[0]


def test_emergency_yellow_red_sequence_and_rear_brake_never_controls_ego(monkeypatch):
    from types import SimpleNamespace as N
    s=scenes.Scene.__new__(scenes.Scene)
    s.config={'kind':'emergency_yellow','signal_trigger_distance_m':30,'minimum_speed_kmh':5,'yellow_duration_s':3}
    s.carla=N(TrafficLightState=N(Yellow='Yellow',Red='Red'),VehicleControl=lambda **kw:kw)
    changes=[];events=[];starts=[];brakes=[]
    s.signal=N(set_state=lambda x:changes.append(x),get_state=lambda:changes[-1] if changes else 'Green')
    s.ego=N(get_transform=lambda:None)  # No apply_control available on purpose.
    s.npc=N(is_alive=True,get_location=lambda:None,get_velocity=lambda:N(length=lambda:10),
            set_autopilot=lambda *args:brakes.append(args),apply_control=lambda value:brakes.append(value))
    position=[-50.,-3.5,0.]
    monkeypatch.setattr(scenes,'relative',lambda *args:position)
    s.state='armed';s.trigger_time=None;s.npc_started=False;s.npc_braked=False
    def start():
        starts.append(True);s.npc_started=True
    s.start_npc=start;s.event=lambda name,row:events.append(name)
    row={'front_gap_to_crossing_m':29,'ego_speed_kmh':30,'ego_route_right_m':0,'carla_elapsed_s':10}
    s.npc_present=lambda:True
    s.sample_extras(dict(row))
    assert changes==['Yellow'] and starts==[True] and s.state=='yellow_siren_cue'
    s.sample_extras({**row,'carla_elapsed_s':12.9})
    assert changes==['Yellow']
    s.sample_extras({**row,'carla_elapsed_s':13.})
    assert changes==['Yellow','Red'] and s.state=='red_rear_approach'
    position[0]=-21
    s.sample_extras({**row,'carla_elapsed_s':14.})
    assert s.npc_braked and brakes==[(False,8000),{'brake':1.}]
    assert events==['yellow_and_rear_approach','signal_red','rear_npc_braking']


def test_missing_npc_keeps_scenario_server_resettable_without_controlling_ego():
    from types import SimpleNamespace as N
    s=scenes.Scene.__new__(scenes.Scene)
    s.npc=N(is_alive=False)
    s.npc_present=lambda:False
    s.state='following';events=[]
    s.event=lambda name,row:events.append(name)
    row={'carla_elapsed_s':20}
    s.sample_extras(row)
    assert s.state=='target_missing' and row['npc_in_ego_m'] is None
    assert events==['scenario_target_missing']
    s.sample_extras(row)
    assert events==['scenario_target_missing']


def test_npc_destroyed_between_liveness_check_and_read_is_recoverable():
    from types import SimpleNamespace as N
    s=scenes.Scene.__new__(scenes.Scene)
    s.npc=N(is_alive=True);s.state='following';s.event=lambda *args:None
    s.npc_present=lambda:s.npc.is_alive
    def removed(row):
        s.npc.is_alive=False
        raise RuntimeError('destroyed actor')
    s._sample_extras=removed
    s.sample_extras({'carla_elapsed_s':20})
    assert s.state=='target_missing'


def test_world_snapshot_rejects_stale_alive_actor_handle():
    from types import SimpleNamespace as N
    s=scenes.Scene.__new__(scenes.Scene)
    s.npc=N(is_alive=True,id=99)
    s.world=N(get_snapshot=lambda:N(find=lambda actor_id:None))
    assert not s.npc_present()


@pytest.mark.parametrize('kind',['pedestrian_lane','rapid_acceleration'])
def test_normal_scene_setup_clears_inherited_rain_and_night(kind):
    from types import SimpleNamespace as N
    scene=scenes.Scene.__new__(scenes.Scene)
    scene.config={'kind':kind};scene.carla=N(WeatherParameters=lambda **kw:N(**kw))
    weather=[N(precipitation=80.,wetness=100.,sun_altitude_angle=-20.,fog_density=10.)]
    scene.world=N(set_weather=lambda value:weather.__setitem__(0,value))
    scene.setup_extras()
    assert weather[0].sun_altitude_angle>0
    assert weather[0].precipitation==weather[0].precipitation_deposits==weather[0].wetness==weather[0].fog_density==0


def test_weather_selection_keeps_only_adverse_scene_rainy_at_night():
    from types import SimpleNamespace as N
    c=N(WeatherParameters=lambda **kw:N(**kw))
    rain=scenes.scenario_weather(c,'rain_night_slow')
    assert rain.precipitation==80 and rain.sun_altitude_angle<0
    clear=scenes.scenario_weather(c,'emergency_yellow')
    assert clear.precipitation==clear.wetness==0 and clear.sun_altitude_angle>0
