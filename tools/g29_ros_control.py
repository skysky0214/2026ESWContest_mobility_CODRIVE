#!/usr/bin/env python3
"""Read G29 inputs and command the local CARLA ROS bridge at 30 Hz."""
import argparse
import fcntl
import json
import os
import signal
import time

os.environ['SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS'] = '1'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
import pygame
import rclpy
from rclpy.signals import SignalHandlerOptions
from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
from std_msgs.msg import Bool, Empty
from sensor_msgs.msg import Joy
from carla_msgs.msg import CarlaEgoVehicleControl, CarlaEgoVehicleStatus
from g29_input import ROOT, CONFIG, load_config, normalize, toggle_direction
from g29_ff import AutoCenter

parser = argparse.ArgumentParser()
parser.add_argument('--probe', action='store_true', help='Print axis values only; send no vehicle control')
args = parser.parse_args()
lock = open(ROOT / 'ros2_bridge/g29.lock', 'w')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Another G29 controller is already running')

pygame.display.init()
pygame.joystick.init()
rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
node = rclpy.create_node('codrive_g29_control')
command_pub = node.create_publisher(CarlaEgoVehicleControl, '/carla/hero/vehicle_control_cmd', 10)
override_pub = node.create_publisher(Bool, '/carla/hero/vehicle_control_manual_override', QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
joy_pub = node.create_publisher(Joy, '/codrive/g29/joy', qos_profile_sensor_data)
status_seen = [0.0, 0.0]
toggle_requests = [0]
node.create_subscription(Empty, '/codrive/g29/toggle_reverse', lambda msg: toggle_requests.__setitem__(0, toggle_requests[0] + 1), 10)
last_toggle_request = 0
def on_vehicle_status(msg):
    status_seen[:] = [time.monotonic(), float(msg.velocity)]
node.create_subscription(CarlaEgoVehicleStatus, '/carla/hero/vehicle_status', on_vehicle_status, qos_profile_sensor_data)
wheel = None
autocenter = AutoCenter()
config = None
armed = False
release_since = None
last_log = last_override = 0.0
config_mtime = 0
next_frame = time.monotonic()
reverse = False
previous_buttons = []

def interrupted(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)

def publish_stop():
    override_pub.publish(Bool(data=False))
    command_pub.publish(CarlaEgoVehicleControl(brake=1.0))

try:
    while rclpy.ok():
        now = time.monotonic()
        events = pygame.event.get()
        disconnected = wheel is not None and any(
            e.type == pygame.JOYDEVICEREMOVED and e.instance_id == wheel.get_instance_id()
            for e in events)
        if wheel is None or disconnected:
            armed = False
            release_since = None
            if wheel is not None:
                print('G29 disconnected: braking, waiting for reconnect.', flush=True)
                autocenter.close()
                wheel.quit()
                wheel = None
            for i in range(pygame.joystick.get_count()):
                candidate = pygame.joystick.Joystick(i)
                if 'G29' in candidate.get_name():
                    wheel = candidate
                    wheel.init()
                    config = load_config(wheel)
                    if not args.probe and not config.get('physical_verified', False):
                        raise RuntimeError('Verify pedal axis mapping before driving (config/g29.json physical_verified).')
                    if not args.probe:
                        autocenter.configure(config.get('autocenter', {}))
                        print('Autocenter: ' + str(autocenter.settings), flush=True)
                    config_mtime = CONFIG.stat().st_mtime_ns
                    previous_buttons = [False] * wheel.get_numbuttons()
                    print('G29 connected: ' + json.dumps(config, ensure_ascii=False), flush=True)
                    print('Release both pedals for one second to enable driving. Steering and pedals are active once enabled.', flush=True)
                    break
        state = {'timestamp': time.time(), 'connected': wheel is not None, 'armed': False, 'steer': 0.0, 'throttle': 0.0, 'brake': 1.0}
        if wheel is not None:
            if CONFIG.stat().st_mtime_ns != config_mtime:
                config = load_config(wheel)
                if not args.probe and not config.get('physical_verified', False):
                    raise RuntimeError('G29 configuration is not physically verified')
                if not args.probe:
                    autocenter.configure(config.get('autocenter', {}))
                config_mtime = CONFIG.stat().st_mtime_ns
                armed = False
                release_since = None
                print('Calibration reloaded. Release pedals to resume.', flush=True)
            axes = [wheel.get_axis(i) for i in range(wheel.get_numaxes())]
            state.update(normalize(axes, config))
            state['raw_axes'] = axes
            buttons = [bool(wheel.get_button(i)) for i in range(wheel.get_numbuttons())]
            if not armed:
                released = state['throttle'] == 0.0 and state['brake'] == 0.0
                release_since = (release_since or now) if released else None
                armed = release_since is not None and now - release_since >= 1.0
            binding = config.get('reverse_toggle', {})
            index = binding.get('button') if binding.get('type') == 'g29_button' else None
            pressed = index is not None and 0 <= index < len(buttons) and buttons[index] and not previous_buttons[index]
            requested = pressed or toggle_requests[0] != last_toggle_request
            last_toggle_request = toggle_requests[0]
            old_direction = reverse
            reverse = toggle_direction(reverse, requested, state['throttle'], status_seen[1], now - status_seen[0] < 2.0)
            if requested:
                print('Direction: ' + ('R' if reverse else 'D') if reverse != old_direction else 'Direction change ignored: stop and release accelerator first.', flush=True)
            previous_buttons = buttons
            state['armed'] = armed
            state['reverse'] = reverse
            state['gear'] = 'R' if reverse else 'D'
            joy = Joy()
            joy.header.stamp = node.get_clock().now().to_msg()
            joy.axes = [float(x) for x in axes]
            joy.buttons = [int(x) for x in buttons]
            joy_pub.publish(joy)
        if not args.probe:
            if now - last_override >= 1:
                override_pub.publish(Bool(data=False))
                last_override = now
            if armed and now - status_seen[0] < 2.0:
                state['applied_throttle']=state['throttle']**float(config.get('driving',{}).get('throttle_exponent',1.))
                command_pub.publish(CarlaEgoVehicleControl(throttle=state['applied_throttle'], brake=state['brake'], steer=state['steer'], reverse=reverse, gear=-1 if reverse else 1))
            else:
                publish_stop()
        if not args.probe:
            autocenter.update_speed(status_seen[1],now-status_seen[0]<2.)
        state['autocenter_strength_percent']=round(autocenter.strength,1)
        state['autocenter_enabled'] = autocenter.fd is not None
        state['ros_status_fresh'] = now - status_seen[0] < 2.0
        if now - last_log >= 0.5:
            print(json.dumps(state, ensure_ascii=False), flush=True)
            tmp = ROOT / 'ros2_bridge/g29-state.tmp'
            tmp.write_text(json.dumps(state, indent=2) + '\n')
            tmp.replace(ROOT / 'ros2_bridge/g29-state.json')
            last_log = now
        rclpy.spin_once(node, timeout_sec=0)
        next_frame += 1 / 30
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_frame = time.monotonic()
except KeyboardInterrupt:
    pass
finally:
    autocenter.close()
    if not args.probe and rclpy.ok():
        for _ in range(8):
            publish_stop()
            rclpy.spin_once(node, timeout_sec=0.03)
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
    pygame.quit()
