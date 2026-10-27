#!/usr/bin/env python3
"""Saved pedestrian scene driven by the user; no automatic throttle or fake DDS inputs."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from carla_pedestrian_scenario import PiRecorder, dump, pose, summarize
from carla_scenario_metrics import TrialMetrics

ROLE = 'codrive_saved_pedestrian_v1'


def validate(config):
    if config.get('schema_version') != 1 or config.get('map') != 'Town10HD_Opt':
        raise ValueError('Expected Town10HD_Opt scenario schema 1')
    if config.get('kind', 'pedestrian') not in ('pedestrian', 'pedestrian_lane', 'rapid_acceleration', 'rain_night_slow', 'emergency_yellow'):
        raise ValueError('Unknown scenario kind')
    kind = config.get('kind', 'pedestrian')
    if kind in ('rain_night_slow', 'emergency_yellow'):
        for key, limits in {'npc_speed_kmh': (5, 60), 'npc_forward_m': (-30, 35),
                            'recommended_speed_kmh': (5, 60)}.items():
            value = config.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not limits[0] <= value <= limits[1]:
                raise ValueError(f'Invalid {key}')
        if kind == 'emergency_yellow':
            if not 15 <= config.get('signal_trigger_distance_m', 0) <= 35 or not 2 <= config.get('yellow_duration_s', 0) <= 5:
                raise ValueError('Invalid traffic light trigger/timing')
            if config.get('stop_road_id') != 7 or config.get('stop_lane_id') != 2:
                raise ValueError('Unexpected saved intersection')
    bounds = {'crossing_forward_m': (25, 65), 'pedestrian_speed_mps': (.2, 5),
              'trigger_ttc_s': (.3, 5), 'trigger_max_distance_m': (3, 40),
              'minimum_speed_kmh': (1, 30), 'recommended_speed_kmh': (5, 80)}
    for key, limits in {'lane_warning_offset_m': (.1, 2), 'acceleration_threshold_mps2': (.5, 10),
                        'accelerator_threshold': (.1, 1)}.items():
        if key in config:
            bounds[key] = limits
    if config.get('trigger_mode', 'ttc') not in ('distance', 'ttc'):
        raise ValueError('Unknown trigger mode')
    if config.get('trigger_mode') == 'distance':
        bounds.update({'trigger_distance_m': (3, 40), 'trigger_lateral_tolerance_m': (1.75, 10)})
    for key, (lo, hi) in bounds.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f'Invalid {key}')
    if not -5 <= config['lateral_start_m'] < -1.5 or not 1.5 < config['lateral_end_m'] <= 5:
        raise ValueError('Crossing must start and end outside the ego lane')
    if isinstance(config['spawn_index'], bool) or not isinstance(config['spawn_index'], int) or config['spawn_index'] < 0:
        raise ValueError('Invalid spawn index')
    return config


def catalog(directory):
    result = {}
    for path in sorted(directory.glob('*.json')):
        config = validate(json.loads(path.read_text()))
        ident = config['id']
        if ident in result:
            raise ValueError(f'Duplicate scenario id: {ident}')
        result[ident] = {'path': path, 'name': config['name'], 'instructions': config.get('instructions', '')}
    return result


def trigger_condition(config, gap_m, forward_speed_mps, ego_lateral_m, lane_width):
    if not all(math.isfinite(v) for v in (gap_m, forward_speed_mps, ego_lateral_m, lane_width)):
        return False
    tolerance = config.get('trigger_lateral_tolerance_m', lane_width / 2)
    timing = (gap_m <= config['trigger_distance_m'] if config.get('trigger_mode') == 'distance'
              else forward_speed_mps > 0 and gap_m / forward_speed_mps <= config['trigger_ttc_s'])
    return (0 < gap_m <= config['trigger_max_distance_m']
            and forward_speed_mps >= config['minimum_speed_kmh'] / 3.6
            and abs(ego_lateral_m) < tolerance and timing)



def scenario_weather(carla, kind):
    """Select scene weather independently of whichever weather was active on startup."""
    rain = kind == 'rain_night_slow'
    return carla.WeatherParameters(
        cloudiness=95. if rain else 10.,
        precipitation=80. if rain else 0.,
        precipitation_deposits=80. if rain else 0.,
        wetness=100. if rain else 0.,
        wind_intensity=10., sun_azimuth_angle=300.,
        sun_altitude_angle=-20. if rain else 60.,
        fog_density=10. if rain else 0.,
        fog_distance=40. if rain else 1000., fog_falloff=.1)


def relative(reference, location):
    inverse = reference.get_inverse_matrix()
    return [sum(inverse[i][j] * v for j, v in enumerate((location.x, location.y, location.z, 1))) for i in range(3)]


class NoPiRecorder:
    """Keep scenario controls without a background observer polling the Pi."""
    def __init__(self):
        self.phase='unrecorded'
        self.thread=self
    def start(self):
        pass
    def close(self):
        pass


class DemoContext:
    """Heartbeat contains only explicit scenario settings, never simulator detections."""
    def __init__(self, config):
        from codrive.transport import CycloneDdsBus
        previous = os.environ.get('CYCLONEDDS_URI')
        try:
            os.environ['CYCLONEDDS_URI'] = config.resolve().as_uri()
            self.bus = CycloneDdsBus(domain_id=42)
        finally:
            # ROS/G29 subprocesses use local domain 26, not the board DDS network.
            if previous is None:
                os.environ.pop('CYCLONEDDS_URI', None)
            else:
                os.environ['CYCLONEDDS_URI'] = previous
        self.sequence = 0
        self.last = 0.

    def publish(self, ident, force=False):
        from codrive.models import Header, ScenarioContext, EnvironmentState, Weather
        from codrive.runtime import publish_model
        if not force and time.monotonic()-self.last < .2:
            return
        self.sequence += 1
        h = Header.now('laptop.carla.scenario', self.sequence)
        env = None
        active = ident if ident in ('rain-night-slow-vehicle','emergency-yellow','pedestrian-lane','rapid-acceleration') else ''
        if active:
            rain=ident=='rain-night-slow-vehicle'
            env = EnvironmentState(h, weather=Weather.RAIN if rain else Weather.CLEAR,
                                   is_night=rain, precipitation_mm_h=5. if rain else 0.,
                                   visibility_m=10000., api_valid=True, daylight_valid=True,
                                   weather_time_ns=h.capture_time_ns,
                                   weather_valid_until_ns=h.capture_time_ns+2_000_000_000)
        publish_model(self.bus, 'scenario_context', ScenarioContext(h, active, env, ident == 'emergency-yellow'))
        self.last = time.monotonic()

    def close(self):
        self.publish('', force=True)
        self.bus.close()


def signal_trigger(config, row):
    return (0 < row['front_gap_to_crossing_m'] <= config['signal_trigger_distance_m']
            and row['ego_speed_kmh'] >= config['minimum_speed_kmh']
            and abs(row['ego_route_right_m']) < 1.75)


class Scene:
    def __init__(self, args):
        import carla
        self.carla, self.args = carla, args
        self.config = validate(json.loads(args.config.read_text()))
        self.client = carla.Client(args.host, args.port)
        self.client.set_timeout(4)
        self.world = self.client.get_world()
        self.map = self.world.get_map()
        if self.map.name.rsplit('/', 1)[-1] != self.config['map']:
            raise RuntimeError('Scenario map mismatch; this tool does not reload the world')
        heroes = [a for a in self.world.get_actors().filter('vehicle.*') if a.attributes.get('role_name') == 'hero']
        if len(heroes) != 1:
            raise RuntimeError('Expected one existing hero vehicle with front camera')
        self.ego = heroes[0]
        if self.ego.get_velocity().length() > .5:
            raise RuntimeError('Stop the ego before opening a saved scene')
        self.start, self.lane_width = self.route(self.config)
        self.npc = None
        self.signal_group = []
        self.hidden_parked_ids = set()
        self.original_weather = self.world.get_weather()
        self.original_ego_lights = self.ego.get_light_state()
        self.context = DemoContext(args.dds_config)
        self.context_error = None
        self.npc_started = False
        self.npc_braked = False
        self.signal = None
        self.pedestrian = None
        self.g29 = None
        self.recorder = self.truth_file = self.events = None
        self.run_dir = None
        self.trial = 0
        self.state = 'setup'
        self.trigger_time = None
        self.closing = 0.0
        self.original_pose = pose(self.ego.get_transform())
        self.metrics = TrialMetrics()

    def route(self, config):
        c = self.carla
        explicit = config.get('start_transform')
        if explicit:
            wp = self.map.get_waypoint(c.Location(x=explicit['x'], y=explicit['y'], z=explicit['z']))
        else:
            wp = self.map.get_waypoint(self.map.get_spawn_points()[config['spawn_index']].location)
        limit = 50 if config.get('kind') == 'emergency_yellow' else 87
        for distance in range(3, limit, 3):
            choices = wp.next(distance)
            if len(choices) != 1:
                raise RuntimeError('Saved straight route is ambiguous')
            q = choices[0]
            yaw = abs((q.transform.rotation.yaw-wp.transform.rotation.yaw+180) % 360-180)
            if q.is_junction or yaw > 2 or abs(q.transform.location.z-wp.transform.location.z) > .15:
                raise RuntimeError('Saved route no longer meets straight/flat conditions')
        if explicit and abs((wp.transform.rotation.yaw-explicit['yaw']+180) % 360-180) > 2:
            raise RuntimeError('Saved intersection direction changed')
        return wp.transform, wp.lane_width

    def cleanup_extras(self):
        hidden=getattr(self,"hidden_parked_ids",set())
        if hidden:
            self.world.enable_environment_objects(hidden,True)
            self.hidden_parked_ids=set()
        if self.npc_present():
            try:
                self.npc.set_autopilot(False, 8000)
                self.npc.destroy()
            except RuntimeError:
                if self.npc_present():
                    raise
        self.npc = None
        for light, state, frozen in self.signal_group:
            if light.is_alive:
                light.set_state(state)
                light.freeze(frozen)
        self.signal_group = []
        self.signal = None
        self.world.set_weather(self.original_weather)
        self.ego.set_light_state(self.original_ego_lights)
        self.npc_started = self.npc_braked = False

    def setup_extras(self):
        kind = self.config.get('kind')
        c = self.carla
        self.world.set_weather(scenario_weather(c, kind))
        if self.config.get('clear_roadside_parked_vehicles', False):
            # Static parked props along this scene's approach; leave live NPCs intact.
            hidden=set()
            for label in (c.CityObjectLabel.Car,c.CityObjectLabel.Truck,c.CityObjectLabel.Bus):
                for item in self.world.get_environment_objects(label):
                    forward,right,_=relative(self.start,item.transform.location)
                    if -15<=forward<=110 and abs(right)<=15:
                        hidden.add(item.id)
            if hidden:self.world.enable_environment_objects(hidden,False)
            self.hidden_parked_ids=hidden
        if kind not in ('rain_night_slow', 'emergency_yellow'):
            return
        if kind == 'rain_night_slow':
            self.ego.set_light_state(c.VehicleLightState(int(self.original_ego_lights) |
                int(c.VehicleLightState.Position) | int(c.VehicleLightState.LowBeam)))
        else:
            matches = [light for light in self.world.get_actors().filter('traffic.traffic_light')
                       if any(wp.road_id == self.config['stop_road_id'] and wp.lane_id == self.config['stop_lane_id']
                              for wp in light.get_stop_waypoints())]
            if len(matches) != 1:
                raise RuntimeError('Saved intersection traffic light not unique')
            self.signal = matches[0]
            stop = next(wp for wp in self.signal.get_stop_waypoints()
                        if wp.road_id == self.config['stop_road_id'] and wp.lane_id == self.config['stop_lane_id'])
            distance = relative(self.start, stop.transform.location)[0]
            if not 55 <= distance <= 75:
                raise RuntimeError('Saved stop line moved')
            self.config = {**self.config, 'crossing_forward_m': distance}
            self.signal_group = [(light, light.get_state(), light.is_frozen())
                                 for light in self.signal.get_group_traffic_lights()]
            for light, _, _ in self.signal_group:
                light.freeze(True)
                light.set_state(c.TrafficLightState.Green if light.id == self.signal.id else c.TrafficLightState.Red)
        bp = self.world.get_blueprint_library().find(self.config['npc_blueprint'])
        bp.set_attribute('role_name', 'codrive_saved_npc_v1')
        location = self.start.transform(c.Location(x=self.config['npc_forward_m'],
                                                  y=-self.lane_width if kind == 'emergency_yellow' else 0., z=.5))
        waypoint = self.map.get_waypoint(location)
        if (waypoint.lane_type != c.LaneType.Driving or
            abs((waypoint.transform.rotation.yaw-self.start.rotation.yaw+180) % 360-180) > 2):
            raise RuntimeError('NPC lane is not a matching driving lane')
        self.npc = self.world.try_spawn_actor(bp, c.Transform(location, self.start.rotation))
        if self.npc is None:
            raise RuntimeError('Scenario NPC spawn blocked; no existing actors were removed')
        self.npc.apply_control(c.VehicleControl(brake=1.))
        lamps = int(c.VehicleLightState.Position) | int(c.VehicleLightState.LowBeam)
        if kind == 'emergency_yellow':
            lamps |= int(c.VehicleLightState.Special1) | int(c.VehicleLightState.Special2)
        self.npc.set_light_state(c.VehicleLightState(lamps))
        self.tm = self.client.get_trafficmanager(8000)
        self.tm.auto_lane_change(self.npc, False)
        self.tm.set_desired_speed(self.npc, self.config['npc_speed_kmh'])

    def start_npc(self):
        self.npc.apply_control(self.carla.VehicleControl(brake=0.))
        self.npc.set_autopilot(True, 8000)
        self.npc_started = True

    def npc_present(self):
        # Actor handles can remain "alive" and return zero/stale cached coordinates
        # after another client destroys them; the world snapshot is authoritative.
        return (self.npc is not None and self.npc.is_alive
                and self.world.get_snapshot().find(self.npc.id) is not None)

    def sample_extras(self, row):
        # Traffic Manager or another CARLA client may remove a scenario actor.
        # Keep the control server alive; never teleport/spawn a replacement while driving.
        if not self.npc_present():
            self.target_missing(row)
            return
        try:
            self._sample_extras(row)
        except RuntimeError:
            if not self.npc_present():
                self.target_missing(row)
            else:
                raise

    def target_missing(self, row):
        if self.state != 'target_missing':
            self.event('scenario_target_missing', {'carla_elapsed_s': row['carla_elapsed_s']})
        self.state = 'target_missing'
        row.update(npc_in_ego_m=None, npc_speed_kmh=None,
                   phone_cue='시나리오 차량이 없어졌습니다. 정차 후 R로 다시 시작하세요',
                   scene_notice='시나리오 차량이 없어졌습니다. 정차 후 R로 다시 시작하세요')

    def _sample_extras(self, row):
        c = self.carla
        kind = self.config['kind']
        p = relative(self.ego.get_transform(), self.npc.get_location())
        row.update(npc_in_ego_m=p, npc_speed_kmh=self.npc.get_velocity().length()*3.6,
                   environment_source='explicit_demo_preset')
        if kind == 'rain_night_slow':
            if not self.npc_started and row['ego_speed_kmh'] >= self.config['minimum_speed_kmh']:
                self.start_npc()
                self.state = 'following'
                self.event('lead_vehicle_started', row)
            row['phone_cue'] = ''
        else:
            if self.state == 'armed' and signal_trigger(self.config, row):
                self.signal.set_state(c.TrafficLightState.Yellow)
                self.trigger_time = row['carla_elapsed_s']
                self.start_npc()
                self.state = 'yellow_siren_cue'
                self.event('yellow_and_rear_approach', {**row, 'siren_detection': 'real_microphone_only'})
            if self.trigger_time is not None and row['carla_elapsed_s']-self.trigger_time >= self.config['yellow_duration_s'] and self.state == 'yellow_siren_cue':
                self.signal.set_state(c.TrafficLightState.Red)
                self.state = 'red_rear_approach'
                self.event('signal_red', row)
            # Stop the NPC while it is still behind the ego. No ego control changes.
            if self.npc_started and not self.npc_braked and p[0] >= -22:
                self.npc.set_autopilot(False, 8000)
                self.npc.apply_control(c.VehicleControl(brake=1.))
                self.npc_braked = True
                self.event('rear_npc_braking', row)
            row['traffic_light_state'] = str(self.signal.get_state())
            row['phone_cue'] = ('지금 휴대폰 사이렌 재생 · 후방 왼쪽 (감지는 실제 마이크)' if self.trigger_time is not None
                                else '황색으로 바뀌면 휴대폰 사이렌을 재생하세요')
            row['rear_outside_forward_view'] = p[0] < 0

    def has_pedestrian(self):
        return self.config.get('kind', 'pedestrian') in ('pedestrian', 'pedestrian_lane')

    def wheel_state(self):
        path = self.args.carla_dir / 'ros2_bridge/g29-state.json'
        try:
            if time.time() - path.stat().st_mtime > 2:
                return {}
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def reset_allowed(self):
        wheel = self.wheel_state()
        if self.ego.get_velocity().length() >= .5:
            return False, '먼저 브레이크로 정차하세요'
        if self.ego.get_control().throttle > .03 or wheel.get('throttle', 0) > .03:
            return False, '가속페달에서 발을 떼세요'
        return True, ''

    def spawn_pedestrian(self):
        existing = [a for a in self.world.get_actors().filter('walker.*') if a.attributes.get('role_name') == ROLE]
        if len(existing) > 1:
            raise RuntimeError('Multiple saved-scene pedestrians; inspect ownership first')
        if existing:
            if existing[0].type_id != self.config['pedestrian_blueprint']:
                raise RuntimeError('Saved-scene pedestrian type mismatch')
            self.pedestrian = existing[0]
        else:
            bp = self.world.get_blueprint_library().find(self.config['pedestrian_blueprint'])
            bp.set_attribute('role_name', ROLE)
            if bp.has_attribute('is_invincible'):
                bp.set_attribute('is_invincible', 'true')
            p = self.start.transform(self.carla.Location(x=self.config['crossing_forward_m'], y=self.config['lateral_start_m'], z=1.0))
            self.pedestrian = self.world.try_spawn_actor(bp, self.carla.Transform(p))
            if self.pedestrian is None:
                raise RuntimeError('Pedestrian spawn blocked')

    def close_trial(self):
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        for name in ('truth_file', 'events'):
            file = getattr(self, name)
            if file:
                file.close()
                setattr(self, name, None)
        if self.run_dir:
            report = ({'pi_recording':False} if self.args.no_pi_recorder else summarize(self.run_dir / 'pi.jsonl'))
            report.update({'scenario_id': self.config['id'], 'trial': self.trial,
                           'last_state': self.state,
                           'reference_events': sorted(self.metrics.observed),
                           'judgment_verification': 'manual_review_required',
                           'note': 'User-operated G29 scene; geometric trigger TTC is not a camera TTC measurement.'})
            dump(self.run_dir / 'summary.json', report)
            self.run_dir = None

    def reset(self, config_path=None):
        allowed, reason = self.reset_allowed()
        if not allowed:
            raise RuntimeError(reason)
        path = config_path or self.args.config
        fresh = validate(json.loads(path.read_text()))
        # Resolve/validate the next route before releasing the current scene.
        start, lane_width = self.route(fresh)
        self.close_trial()
        self.cleanup_extras()
        if self.pedestrian:
            self.pedestrian.destroy()
            self.pedestrian = None
        self.config = fresh
        self.args.config = path
        self.start, self.lane_width = start, lane_width
        self.metrics = TrialMetrics()
        if self.has_pedestrian():
            self.spawn_pedestrian()
        c = self.carla
        self.state = 'resetting'
        self.ego.disable_constant_velocity()
        self.ego.apply_control(c.VehicleControl(brake=1.0))
        self.ego.set_target_velocity(c.Vector3D())
        self.ego.set_target_angular_velocity(c.Vector3D())
        start = c.Transform(c.Location(self.start.location.x, self.start.location.y, self.start.location.z + .25), self.start.rotation)
        self.ego.set_transform(start)
        if self.pedestrian:
            self.pedestrian.apply_control(c.WalkerControl(speed=0.0))
            p = self.start.transform(c.Location(x=self.config['crossing_forward_m'], y=self.config['lateral_start_m']))
            p.z = self.start.location.z + self.pedestrian.bounding_box.extent.z + .03
            self.pedestrian.set_transform(c.Transform(p, c.Rotation(yaw=self.start.rotation.yaw + 90)))
        # No fixed velocity, automatic throttle, map reload or global synchronous-mode changes.
        deadline = time.monotonic() + .8
        while time.monotonic() < deadline:
            time.sleep(.05)
        try:
            self.setup_extras()
        except Exception:
            self.cleanup_extras()
            raise
        self.context.publish(self.config['id'], force=True)
        self.trial += 1
        self.trigger_time = None
        self.state = 'armed'
        self.run_dir = self.args.output / f'trial-{self.trial:03d}-{time.time_ns()}'
        self.run_dir.mkdir(parents=True)
        dump(self.run_dir / 'scenario.json', self.config)
        dump(self.run_dir / 'setup.json', {'ego_id': self.ego.id, 'pedestrian_id': self.pedestrian.id if self.pedestrian else None,
                                          'start_pose': pose(self.start), 'original_ego_pose': self.original_pose})
        self.truth_file = (self.run_dir / 'ground-truth.jsonl').open('w')
        self.events = (self.run_dir / 'events.jsonl').open('w')
        self.recorder = (NoPiRecorder() if self.args.no_pi_recorder else PiRecorder(self.args.pi_url, self.run_dir / 'pi.jsonl'))
        self.recorder.phase = 'armed'
        self.recorder.thread.start()

    def event(self, name, data):
        row = {'event': name, 'wall_time_ns': time.time_ns(), **data}
        self.events.write(json.dumps(row) + '\n')
        self.events.flush()

    def fire(self, mode, data):
        if not self.pedestrian:
            raise RuntimeError('급가속 시나리오는 보행자를 사용하지 않습니다')
        if self.state != 'armed':
            raise RuntimeError('이미 발동된 장면입니다. 정차 후 초기화하세요')
        direction = self.start.get_right_vector()
        self.pedestrian.apply_control(self.carla.WalkerControl(direction=direction, speed=self.config['pedestrian_speed_mps']))
        self.trigger_time = self.world.get_snapshot().timestamp.elapsed_seconds
        self.state = 'crossing'
        self.recorder.phase = 'crossing'
        self.event('pedestrian_trigger', {'trigger_mode': mode, **data})

    def sample(self):
        snap = self.world.get_snapshot()
        ego_pose = self.ego.get_transform()
        ego_position = relative(self.start, ego_pose.location)
        ped_position = relative(self.start, self.pedestrian.get_location()) if self.pedestrian else None
        crossing_point = self.start.transform(self.carla.Location(x=self.config['crossing_forward_m']))
        point_from_ego = relative(ego_pose, crossing_point)
        velocity = self.ego.get_velocity()
        forward = self.start.get_forward_vector()
        speed_forward = velocity.x*forward.x + velocity.y*forward.y + velocity.z*forward.z
        gap = self.config['crossing_forward_m'] - ego_position[0] - (self.ego.bounding_box.location.x + self.ego.bounding_box.extent.x)
        row = {'wall_time_ns': time.time_ns(), 'carla_frame': snap.frame,
               'carla_elapsed_s': snap.timestamp.elapsed_seconds, 'state': self.state,
               'ego_speed_kmh': velocity.length()*3.6, 'forward_speed_mps': speed_forward,
               'ego_route_forward_m': ego_position[0], 'ego_route_right_m': ego_position[1],
               'front_gap_to_crossing_m': gap,
               'geometric_ttc_to_crossing_s': gap/speed_forward if gap > 0 and speed_forward > .1 else None,
               'pedestrian_route_right_m': ped_position[1] if ped_position else None,
               'pedestrian_in_ego_m': relative(ego_pose, self.pedestrian.get_location()) if self.pedestrian else None,
               'crossing_point_in_ego_m': point_from_ego, 'control': {'throttle': self.ego.get_control().throttle,
               'brake': self.ego.get_control().brake, 'steer': self.ego.get_control().steer}}
        if self.config.get('kind') in ('rain_night_slow', 'emergency_yellow'):
            self.sample_extras(row)
        elif not self.has_pedestrian():
            row['state'] = self.state
            for name in self.metrics.observe(self.config, row):
                self.event(name, row)
                self.state = 'acceleration_observed'
            if self.state == 'acceleration_observed' and velocity.length() < .5 and row['control']['throttle'] <= .03:
                self.state = 'finished'
                self.event('rapid_trial_finished', row)
            elif self.state == 'armed' and ego_position[0] > 75:
                self.state = 'missed'
                self.event('rapid_condition_not_observed', row)
        elif self.state == 'armed':
            if trigger_condition(self.config, gap, speed_forward, ego_position[1], self.lane_width):
                self.fire('automatic_geometry', row)
            elif gap < -5:
                self.state = 'missed'
                self.event('trigger_missed', row)
        elif self.state == 'crossing':
            completed = ped_position[1] >= self.config['lateral_end_m']
            if completed or snap.timestamp.elapsed_seconds-self.trigger_time > 10:
                self.pedestrian.apply_control(self.carla.WalkerControl(speed=0.0))
                self.state = 'finished' if completed else 'timed_out'
                self.recorder.phase = self.state
                self.event('crossing_finished' if completed else 'crossing_timed_out', row)
        if self.has_pedestrian():
            row['state'] = self.state
            for name in self.metrics.observe(self.config, row):
                self.event(name, row)
        self.context.publish(self.config['id'])
        row['demo_context_error'] = self.context_error
        self.recorder.phase = self.state
        self.truth_file.write(json.dumps(row) + '\n')
        self.truth_file.flush()
        row.update({'state': self.state, 'trial': self.trial, 'scenario': self.config['name'],
                    'scenario_id': self.config['id'], 'kind': self.config.get('kind', 'pedestrian'),
                    'instructions': self.config.get('instructions', ''),
                    'reference_events': sorted(self.metrics.observed),
                    'recommended_speed_kmh': self.config['recommended_speed_kmh'],
                    'g29': self.wheel_state(), 'output': str(self.run_dir)})
        return row

    def start_g29(self):
        if self.args.no_g29:
            return
        lock_path = self.args.carla_dir / 'ros2_bridge/g29.lock'
        with lock_path.open('a') as check:
            try:
                fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Using existing G29 controller', flush=True)
                return
        env = os.environ.copy()
        script = self.args.carla_dir / 'tools/g29_ros_control.py'
        self.g29_log = (self.args.output / 'g29.log').open('a')
        self.g29 = subprocess.Popen([str(self.args.carla_dir / '.venv/bin/python'), str(script)],
                                    cwd=self.args.carla_dir, env=env, stdout=self.g29_log,
                                    stderr=self.g29_log, start_new_session=True)

    def close(self):
        self.context.close()
        self.cleanup_extras()
        if self.g29:
            self.g29.terminate()
            try:
                self.g29.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.g29.kill()
                self.g29.wait()
            self.g29_log.close()
        if self.pedestrian and self.pedestrian.is_alive:
            self.pedestrian.apply_control(self.carla.WalkerControl(speed=0.0))
            if self.pedestrian.attributes.get('role_name') == ROLE:
                self.pedestrian.destroy()
        self.close_trial()


PAGE = '''<!doctype html><meta charset="utf-8"><title>CARLA 시나리오</title>
<style>body{font:20px sans-serif;background:#15212b;color:#eee;max-width:780px;margin:40px auto}button{font-size:21px;padding:16px;margin:8px 0}pre{font-size:17px;white-space:pre-wrap}</style>
<h1 id="title">저장 시나리오</h1><p>G29 핸들·페달로 직접 운전하세요.</p>
<select id="scenario" style="font-size:20px;padding:12px"></select>
<button onclick="selectScene()">선택한 시나리오로 시작</button><p id="instructions"></p>
<button onclick="resetScene()">시작 위치로 초기화 · 재시험</button>
<button onclick="fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})">시험 종료</button>
<p>정차하고 엑셀에서 발을 뗀 뒤 초기화하세요.<br>CARLA 전방 화면에서는 R키도 사용할 수 있습니다.</p>
<pre id="status">연결 중</pre><p id="message"></p>
<script>
async function resetScene(){let r=await fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});document.querySelector('#message').textContent=(await r.json()).message;}
async function selectScene(){let r=await fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:document.querySelector('#scenario').value})});document.querySelector('#message').textContent=(await r.json()).message;}
async function loadScenes(){let rows=await(await fetch('/api/scenarios')).json();for(let s of rows){let o=document.createElement('option');o.value=s.id;o.textContent=s.name;document.querySelector('#scenario').appendChild(o);}}
async function tick(){try{let s=await(await fetch('/api/state')).json();document.querySelector('#title').textContent=s.scenario??'저장 시나리오';document.querySelector('#instructions').textContent=s.instructions??'';document.querySelector('#status').textContent=`시험 ${s.trial??'-'} · ${s.state??'준비 중'}\n속도 ${(s.ego_speed_kmh??0).toFixed(1)} km/h\n출발 차선 중심 대비 ${(s.ego_route_right_m??0).toFixed(2)} m (왼쪽 음수)\n차속 기반 가속도 ${s.reference_acceleration_mps2==null?'수집 중':s.reference_acceleration_mps2.toFixed(2)+' m/s²'}\n${s.kind==='rapid_acceleration'?'엑셀 90% 이상 + 가속도 4m/s² 이상 + 브레이크 해제':(s.kind==='emergency_yellow' ? ('신호 '+s.traffic_light_state+' · 정지선 '+(s.front_gap_to_crossing_m??0).toFixed(1)+' m\n'+(s.phone_cue??'')) : s.kind==='rain_night_slow' ? ('우천·야간 고정 시연 · 선행차 '+(s.npc_speed_kmh??0).toFixed(1)+' km/h · 전방 '+(s.npc_in_ego_m?.[0]??0).toFixed(1)+' m') : ('횡단 지점까지 '+(s.front_gap_to_crossing_m??0).toFixed(1)+' m'))}\n장면 조건 기록: ${(s.reference_events??[]).join(', ')||'아직 없음'}\nG29 ${s.g29?.connected?'연결됨':'미연결'} · ${s.g29?.armed?'운전 가능':'양쪽 페달을 1초간 떼세요'}\n장면 조건 기록은 Pi 경고 성공 판정과 별도입니다.`;document.querySelector('#message').textContent=s.message??'';}catch(e){document.querySelector('#message').textContent='시나리오 프로그램 연결 끊김';}}
loadScenes();
setInterval(tick,500);tick();</script>'''


def make_server(port, shared, commands, scenarios=None):
    scenarios = scenarios or {}
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, value, content_type='application/json'):
            data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/':
                self.respond(200, PAGE, 'text/html')
            elif self.path == '/api/state':
                self.respond(200, shared['status'])
            elif self.path == '/api/scenarios':
                self.respond(200, [{'id': key, 'name': value['name'], 'instructions': value['instructions']}
                                   for key, value in scenarios.items()])
            else:
                self.respond(404, {'message': 'Not found'})

        def do_POST(self):
            if self.headers.get('Content-Type', '') != 'application/json':
                self.respond(415, {'message': 'Expected JSON'})
                return
            if self.headers.get('Origin') not in (None, f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                self.respond(403, {'message': 'Local control only'})
                return
            if self.path not in ('/api/reset', '/api/test-event', '/api/stop', '/api/select'):
                self.respond(404, {'message': 'Not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 256:
                    raise ValueError('Invalid body size')
                data = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('Expected object')
                ident = data.get('id')
                if self.path == '/api/select' and (not isinstance(ident, str) or ident not in scenarios):
                    raise ValueError('Unknown scenario')
            except (ValueError, TypeError):
                self.respond(400, {'message': '잘못된 시나리오 요청입니다'})
                return
            try:
                commands.put_nowait((self.path.removeprefix('/api/'), ident))
            except queue.Full:
                self.respond(409, {'message': '이전 요청 처리 중'})
                return
            self.respond(202, {'message': '요청을 전달했습니다. 상태를 확인하세요'})

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config/carla/scenarios/pedestrian-crossing.json'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=2000)
    parser.add_argument('--control-port', type=int, default=18081)
    parser.add_argument('--pi-url', default='http://192.168.99.21:8080')
    parser.add_argument('--carla-dir', type=Path, default=Path('../carla').resolve())
    parser.add_argument('--output', type=Path, default=Path('build/carla-saved-scenarios'))
    parser.add_argument('--dds-config', type=Path, default=Path('config/integration/cyclonedds-laptop-wired.xml'))
    parser.add_argument('--no-pi-recorder', action='store_true', help='Keep scene controls without polling the Pi state API')
    parser.add_argument('--no-g29', action='store_true', help='Scene/recording smoke test only; does not drive ego')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with (args.output / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        commands = queue.Queue(maxsize=1)
        shared = {'status': {'state': 'setup'}}
        # Bind before touching any actors, so a second controller cannot reposition ego.
        scenarios = catalog(args.config.parent)
        server = make_server(args.control_port, shared, commands, scenarios)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        scene = None
        try:
            thread.start()
            scene = Scene(args)
            scene.reset()
            scene.start_g29()
            message = '양쪽 페달을 1초간 떼면 운전을 시작할 수 있습니다'
            print(f'Saved scene ready: http://127.0.0.1:{args.control_port}/', flush=True)
            while not stop.is_set():
                if scene.g29 is not None and scene.g29.poll() is not None:
                    raise RuntimeError('G29 controller exited; inspect g29.log')
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    command = None
                if command:
                    try:
                        command, ident = command
                        if command == 'stop':
                            stop.set()
                            break
                        if command in ('reset', 'select'):
                            scene.reset(scenarios[ident]['path'] if command == 'select' else None)
                            message = '초기화 완료. 같은 장면을 다시 운전하세요'
                        else:
                            allowed, reason = scene.reset_allowed()
                            if not allowed:
                                raise RuntimeError(reason)
                            scene.fire('manual_motion_test', {})
                            message = '보행자 동작만 시험 중: 자동 발동 조건 검증과 별개입니다'
                    except (RuntimeError, ValueError, OSError) as exc:
                        message = str(exc)
                        if scene.truth_file is None:
                            print('Scene setup failed: '+message, flush=True)
                            stop.set()
                            break
                status = scene.sample()
                status.update({'message': status.get('scene_notice') or message, 'trigger_ttc_s': scene.config['trigger_ttc_s'],
                               'trigger_mode': scene.config.get('trigger_mode', 'ttc'),
                               'trigger_distance_m': scene.config.get('trigger_distance_m')})
                shared['status'] = status
                stop.wait(.05)
        finally:
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=3)
            server.server_close()
            if scene:
                scene.close()


if __name__ == '__main__':
    main()
