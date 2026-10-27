"""Publish CAN vehicle state only from STM32's actual UART receive reports."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import os
import signal
import time
from pathlib import Path
from threading import Event
from ..hardware.stm32_can import EcuTelemetry
from ..models import Header
from ..runtime import publish_model
from ..transport import CycloneDdsBus

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port')
    parser.add_argument('--baudrate', type=int, default=115200)
    parser.add_argument('--domain-id', type=int, default=42)
    parser.add_argument('--dds-config', default='config/integration/cyclonedds-pi-wired.xml')
    parser.add_argument('--stale-ms', type=int, default=500)
    parser.add_argument('--status-file', type=Path, default=Path('logs/can-ecu-status.json'))
    args = parser.parse_args()
    if args.stale_ms <= 0: parser.error('--stale-ms must be positive')
    import serial
    from serial.tools import list_ports
    os.environ['CYCLONEDDS_URI'] = Path(args.dds_config).resolve().as_uri()
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):signal.signal(sig, lambda *_: stop.set())
    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    bus = CycloneDdsBus(domain_id=args.domain_id)
    port = None
    sequence = rejected = reports = 0
    last_report = 0.0
    last_log = 0.0
    try:
        while not stop.is_set():
            try:
                if port is None:
                    devices = [p.device for p in list_ports.comports() if p.vid == 0x0483 and ('STLink' in p.description or 'ST-LINK' in p.description or p.pid == 0x374e)]
                    selected = args.port or (devices[0] if len(devices) == 1 else None)
                    if selected is None:
                        print('Waiting for one CAN ECU ST-LINK USB UART', flush=True)
                        stop.wait(2);continue
                    port = serial.Serial(selected, args.baudrate, timeout=.1, write_timeout=1, exclusive=True)
                    port.reset_input_buffer()
                    print('CAN ECU UART: '+selected, flush=True)
                raw = port.read_until(b'\n', 4096)
                if not raw:
                    if time.monotonic() - last_report > 2:
                        print('Waiting for actual ECU telemetry; not publishing vehicle state', flush=True)
                        last_report = time.monotonic()
                    continue
                if not raw.endswith(b'\n'):
                    rejected += 1;continue
                try:
                    sample = EcuTelemetry.parse(raw.decode('ascii'))
                except (UnicodeError,ValueError,KeyError):
                    rejected += 1;continue
                received_ns=time.time_ns()
                sequence += 1;reports += 1;last_report=time.monotonic()
                # Ages are ECU uptime differences, not host wall-clock timestamps.
                # Preserve real measurement age instead of refreshing old CAN values.
                header=Header.now('pi5.can_ecu',sequence,frame_id='vehicle',capture_time_ns=received_ns)
                state=sample.to_vehicle_state(header,stale_ms=args.stale_ms)
                publish_model(bus,'vehicle_state',state)
                status={'reports':reports,'rejected_uart':rejected,'received_time_ns':received_ns,
                        'ecu':sample.values,'ages_ms':sample.ages_ms,'vehicle':asdict(state),
                        'unavailable_signals':(['accelerator_pct'] if 'accel_raw' not in sample.values else []) + ['gear','turn_signal','hazard_lights','headlights_on','wiper_level']}
                tmp=args.status_file.with_suffix('.tmp');tmp.write_text(json.dumps(status,indent=2)+'\n');tmp.replace(args.status_file)
                if time.monotonic()-last_log>=1:
                    print(json.dumps(status,ensure_ascii=False),flush=True);last_log=time.monotonic()
            except (serial.SerialException,OSError) as error:
                print('CAN ECU disconnected: '+str(error),flush=True)
                if port is not None:port.close();port=None
                stop.wait(1)
    finally:
        if port is not None:port.close()
        bus.close()
if __name__=='__main__':main()
