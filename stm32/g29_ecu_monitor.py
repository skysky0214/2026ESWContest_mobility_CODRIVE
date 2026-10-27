"""Display VehicleState actually reported by STM32 over ST-LINK UART.

Requires the updated firmware. Does not infer ECU reception from PC transmissions.
py -3.12 E:\\stm32\\g29_ecu_monitor.py --port COM7
"""

import argparse
import time


def parse_telemetry(line):
    if not line.startswith("ECU,"):
        raise ValueError("Not an ECU telemetry line")
    fields = dict(item.split("=", 1) for item in line.strip().split(",")[1:])
    required = ("ms", "speed_raw", "brake", "steer_raw", "rx", "valid", "speed_n", "brake_n",
                "steer_n", "speed_ms", "brake_ms", "steer_ms", "err", "invalid", "lost", "bo", "lec")
    result = {key: int(fields[key]) for key in required}
    if not 0 <= result["speed_raw"] <= 65535 or result["brake"] not in (0, 1) or not -32768 <= result["steer_raw"] <= 32767:
        raise ValueError("Invalid ECU values")
    return result


def format_telemetry(values):
    v = values
    ages = [(v["ms"] - v[key + "_ms"]) & 0xFFFFFFFF for key in ("speed", "brake", "steer")]
    fresh = all(v[k + "_n"] > 0 for k in ("speed", "brake", "steer")) and max(ages) <= 1000
    return (f"ECU {'LIVE' if fresh else 'NO CAN / STALE'} | "
            f"speed={v['speed_raw'] * .01:7.2f} km/h brake={v['brake']} "
            f"steering={v['steer_raw'] * .1:+7.1f} deg | RX={v['rx']} valid={v['valid']} "
            f"IDs={v['speed_n']}/{v['brake_n']}/{v['steer_n']} age_ms={ages} | "
            f"HALerr={v['err']} invalid={v['invalid']} lost={v['lost']} busoff={v['bo']} LEC={v['lec']}")


def main():
    import serial
    from serial.tools import list_ports
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="ST-LINK COM port, auto-detected if omitted")
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()
    ports = [p for p in list_ports.comports() if (p.vid, p.pid) == (0x0483, 0x374E)
             and (args.port is None or p.device.upper() == args.port.upper())]
    if len(ports) != 1:
        parser.error("Connect one NUCLEO ST-LINK or select --port COM7; close other serial monitors")
    print(f"STM32 actual RX/VehicleState monitor: {ports[0].device}, 115200 8N1", flush=True)
    print("Close keyboard_test.py/other COM7 monitors first. Ctrl+C exits.", flush=True)
    started = last_valid = last_warning = time.monotonic()
    buffer = bytearray()
    with serial.Serial(ports[0].device, 115200, timeout=.1) as port:
        while not args.seconds or time.monotonic() - started < args.seconds:
            buffer.extend(port.read(min(max(port.in_waiting, 1), 4096)))
            while b"\n" in buffer:
                raw, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                try:
                    values = parse_telemetry(raw.decode("ascii"))
                except (UnicodeError, ValueError, KeyError):
                    continue
                last_valid = time.monotonic()
                print(format_telemetry(values), flush=True)
            if len(buffer) > 4096:
                buffer.clear()
            now = time.monotonic()
            if now - last_valid > 2 and now - last_warning > 2:
                print("NO ECU TELEMETRY: flash updated firmware and Resume CPU; check COM port.", flush=True)
                last_warning = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as error:
        raise SystemExit(f"ECU monitor stopped: {error}") from error
