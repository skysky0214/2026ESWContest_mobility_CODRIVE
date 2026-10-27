"""G29 discovery and configurable linear normalization; no ROS dependency."""
import array
import fcntl
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get('CODRIVE_CARLA_DIR', str(Path(__file__).resolve().parents[2] / 'carla')))
CONFIG = Path(__file__).resolve().parent.parent / 'config/g29.json'

def clamp(value, low, high):
    return max(low, min(high, value))

def normalize(axes, config):
    steering = config['steering']
    raw = axes[steering['axis']]
    center = steering['center']
    limit = steering['right'] if raw >= center else steering['left']
    steer = (raw - center) / abs(limit - center)
    if abs(steer) < steering['deadzone']:
        steer = 0.0
    steer = clamp(steer, -1.0, 1.0)
    if steering.get('invert', False):
        steer = -steer
    result = {'steer': steer, 'steering_deg': steer * steering['max_degrees']}
    for key in ('throttle', 'brake'):
        pedal = config[key]
        value = (axes[pedal['axis']] - pedal['released']) / (pedal['pressed'] - pedal['released'])
        result[key] = clamp(value, 0.0, 1.0) if value > pedal['deadzone'] else 0.0
    return result

def validate(config, axis_count):
    indices = [config[key]['axis'] for key in ('steering', 'throttle', 'brake')]
    if len(set(indices)) != 3 or any(not 0 <= i < axis_count for i in indices):
        raise ValueError('Steering, throttle and brake need three distinct valid axes')
    steering = config['steering']
    if not steering['left'] < steering['center'] < steering['right'] or steering['max_degrees'] <= 0:
        raise ValueError('Invalid steering calibration')
    for key in ('throttle', 'brake'):
        if abs(config[key]['pressed'] - config[key]['released']) < 0.5:
            raise ValueError('Pedal calibration endpoints are too close')

def load_config(joystick):
    if not CONFIG.exists():
        # Resolve actual kernel joystick axis indices, rather than assuming SDL indices.
        devices = list(Path('/dev/input/by-id').glob('*G29*-joystick'))
        devices = [p for p in devices if 'event-joystick' not in p.name]
        if len(devices) != 1:
            raise RuntimeError('Expected one Linux G29 joystick device')
        with devices[0].open('rb', buffering=0) as device:
            mapping = array.array('B', [0] * 64)
            fcntl.ioctl(device, 0x80406a32, mapping, True)  # JSIOCGAXMAP
        codes = list(mapping[:joystick.get_numaxes()])
        # Linux Logitech G29 native mode: ABS_X steering, ABS_Z throttle, ABS_RZ brake.
        # Reject PS4 generic-gamepad mode, where those labels mean different inputs.
        sys_devices = list(Path('/sys/bus/hid/devices').glob('*:046D:C24F.*'))
        if not sys_devices or any(code not in codes for code in (0, 2, 5)):
            raise RuntimeError('G29 native wheel mode not found. Select PS3 and reconnect USB.')
        config = {
            'name': joystick.get_name(), 'guid': joystick.get_guid(),
            'axis_codes': codes, 'mapping_source': 'Linux native G29 ABS_X/ABS_Z/ABS_RZ; physical input check required',
            'physical_verified': False,
            'steering': {'axis': codes.index(0), 'left': -1.0, 'center': 0.0, 'right': 1.0, 'deadzone': 0.015, 'max_degrees': 450.0},
            'throttle': {'axis': codes.index(2), 'released': 1.0, 'pressed': -1.0, 'deadzone': 0.03},
            'brake': {'axis': codes.index(5), 'released': 1.0, 'pressed': -1.0, 'deadzone': 0.03},
        }
        CONFIG.write_text(json.dumps(config, indent=2) + '\n')
    else:
        config = json.loads(CONFIG.read_text())
        if config['guid'] != joystick.get_guid():
            raise RuntimeError('G29 mode/GUID changed. Recheck config/g29.json before driving.')
    validate(config, joystick.get_numaxes())
    return config

def toggle_direction(reverse, requested, throttle, speed_mps, fresh=True):
    """Toggle R/D once per press, only while stopped with accelerator released."""
    if requested and fresh and throttle <= 0.03 and abs(speed_mps) < 0.5:
        return not reverse
    return reverse
