"""G29 turn switches -> CARLA lamps and explicit DDS intent, without steering/pedal control."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import struct
import time

from ..models import Header, TurnIndicatorState, TurnSignal
from ..runtime import publish_model
from ..transport import CycloneDdsBus
from ..turn_indicators import IndicatorSwitch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config/g29-indicators.json'))
    parser.add_argument('--dds-config', type=Path, default=Path('config/integration/cyclonedds-laptop-wired.xml'))
    parser.add_argument('--carla-host', default='127.0.0.1')
    parser.add_argument('--carla-port', type=int, default=2000)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    switch = IndicatorSwitch(config['left_button'], config['right_button'])
    lock = open('/tmp/codrive-g29-indicators.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    import carla
    os.environ['CYCLONEDDS_URI'] = args.dds_config.resolve().as_uri()
    bus = CycloneDdsBus(domain_id=42)
    client = carla.Client(args.carla_host, args.carla_port)
    client.set_timeout(.5)
    actor, fd, connected = None, None, False
    next_connect = next_actor = next_publish = 0
    sequence = 0
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    mask = int(carla.VehicleLightState.LeftBlinker) | int(carla.VehicleLightState.RightBlinker)
    lamp_error = None
    def lamps(direction):
        if actor is None:
            return False
        current = int(actor.get_light_state())
        flag = (int(carla.VehicleLightState.LeftBlinker) if direction == TurnSignal.LEFT else
                int(carla.VehicleLightState.RightBlinker) if direction == TurnSignal.RIGHT else 0)
        wanted = (current & ~mask) | flag
        if wanted != current:
            actor.set_light_state(carla.VehicleLightState(wanted))
        return True
    try:
        while running:
            now = time.monotonic()
            if fd is None and now >= next_connect:
                try:
                    fd = os.open(config['device'], os.O_RDONLY | os.O_NONBLOCK)
                    name = bytearray(128)
                    fcntl.ioctl(fd, 0x80806a13, name)  # JSIOCGNAME(128), not keyboard input.
                    if b'G29' not in name:
                        raise OSError('Configured joystick is not a G29')
                    switch.reset()
                    connected = True
                    print('G29 indicator buttons ready: left=5, right=4', flush=True)
                except OSError as exc:
                    if fd is not None: os.close(fd)
                    fd, connected = None, False
                    next_connect = now + 2
                    print('Waiting for G29: '+str(exc), flush=True)
            if fd is not None:
                try:
                    if select.select([fd], [], [], .02)[0]:
                        data = os.read(fd, 8)
                        if len(data) != 8: raise OSError('G29 disconnected')
                        _, value, kind, button = struct.unpack('<IhBB', data)
                        if kind == 1:  # Ignore initialization/axis events and held-button replay.
                            before = switch.state
                            switch.update(button, bool(value))
                            if before != switch.state:
                                print('Turn indicator: '+switch.state.value, flush=True)
                except OSError:
                    os.close(fd)
                    fd, connected = None, False
                    switch.reset()
            else:
                time.sleep(.02)
            if now >= next_actor:
                try:
                    heroes = [a for a in client.get_world().get_actors().filter('vehicle.*')
                              if a.attributes.get('role_name') == 'hero']
                    actor = heroes[0] if len(heroes) == 1 else None
                    lamp_error = None if actor else 'No unique hero vehicle'
                except RuntimeError as exc:
                    actor, lamp_error = None, str(exc)
                next_actor = now + 1
            if now >= next_publish:
                try:
                    applied = lamps(switch.state)
                except RuntimeError as exc:
                    applied, actor, lamp_error = False, None, str(exc)
                sequence += 1
                state = TurnIndicatorState(Header.now('laptop.g29.indicators', sequence), switch.state, connected)
                publish_model(bus, 'turn_indicator_state', state)
                status = {'time_ns':time.time_ns(), 'connected':connected, 'turn_signal':switch.state.value,
                          'left_button':switch.left_button, 'right_button':switch.right_button,
                          'carla_applied':applied, 'carla_error':lamp_error,
                          'source':'G29 switch via DDS (not CAN ECU)'}
                target=Path('logs/g29-indicators-status.json')
                target.parent.mkdir(exist_ok=True)
                tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(status,indent=2)+'\n');tmp.replace(target)
                next_publish = now + .1
    finally:
        try: lamps(TurnSignal.OFF)
        except RuntimeError: pass
        if fd is not None: os.close(fd)
        bus.close()


if __name__ == '__main__':
    main()
