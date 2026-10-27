"""CODRIVE CAN encoder and verified CANable2 SLCAN transport.

Default is dry-run. --send explicitly enables Classic CAN on the CANable2 adapter.
Throttle remains an independent local value; virtual speed is explicitly supplied.
"""

import argparse
import math
from pathlib import Path
import struct
import time

from g29_test import CONFIG, Wheel, finite, load_config, normalize

BITRATE = 500_000


class CanableTransport:
    """Transport selected from actual USB descriptor and V-command response.

    A successful send means submitted to the serial adapter, NOT bus ACK or ECU RX.
    This firmware does not acknowledge individual SLCAN transmit commands.
    """
    def __init__(self, port=None):
        import can
        import serial
        from serial.tools import list_ports
        self.can = can
        self.bus = None
        self.submitted = 0
        matches = [p for p in list_ports.comports() if (p.vid, p.pid) == (0x16D0, 0x117E)
                   and (port is None or p.device.upper() == port.upper())]
        if len(matches) != 1:
            raise ValueError("Connect one CANable2 (VID 16D0 PID 117E), or select it with --port COMx. ST-LINK is not CAN.")
        self.port = matches[0].device
        # Query version without opening a CAN channel or transmitting a data frame.
        with serial.Serial(self.port, 115200, timeout=.3, write_timeout=1) as probe:
            time.sleep(.3)
            probe.reset_input_buffer()
            probe.write(b"V\r")
            probe.flush()
            reply = probe.read_until(b"\r", 256).decode("ascii", errors="replace").strip()
        if "normaldotcom/canable2" not in reply:
            raise ValueError(f"Unverified adapter firmware response: {reply!r}")
        print(f"Adapter: {self.port}; USB serial={matches[0].serial_number}; firmware={reply}", flush=True)
        self.bus = can.Bus(interface="slcan", channel=self.port, tty_baudrate=115200,
                           bitrate=BITRATE, sleep_after_open=.3, timeout=.05, ignore_config=True)
        try:
            # CANable firmware-specific documented commands; default retransmission
            # disabled to avoid accumulating old frames during a missing-ACK test.
            self.bus._write("C")
            self.bus._write("M0")
            self.bus._write("A0")
            self.bus._write("S6")
            self.bus._write("O")
        except Exception:
            self.bus.shutdown()
            self.bus = None
            raise

    def send_frames(self, frames):
        for identifier, data in frames:
            msg = self.can.Message(arbitration_id=identifier, data=data, is_extended_id=False,
                                   is_fd=False, bitrate_switch=False, check=True)
            self.bus.send(msg, timeout=.1)
            self.submitted += 1

    def close(self):
        if self.bus:
            self.bus.shutdown()
            self.bus = None


def encode_frames(speed_kph, brake_pressed, steering_deg):
    """Return (standard ID, eight data bytes); unused bytes are zero."""
    speed_kph, steering_deg = finite(speed_kph), finite(steering_deg)
    if not 0 <= speed_kph <= 655.35:
        raise ValueError("Speed must be within uint16 range: 0..655.35 km/h")
    if not -450 <= steering_deg <= 450:
        raise ValueError("Steering must be -450..450 degrees")
    if type(brake_pressed) not in (bool, int) or brake_pressed not in (0, 1):
        raise ValueError("Brake must be 0 or 1")
    return [
        (0x100, struct.pack("<H", round(speed_kph * 100)) + bytes(6)),
        (0x101, bytes([int(brake_pressed)]) + bytes(7)),
        (0x102, struct.pack("<h", round(steering_deg * 10)) + bytes(6)),
    ]


def decode_frames(frames):
    """PC-side preview of the ECU wire rules; this is not received ECU telemetry."""
    data = dict(frames)
    if set(data) != {0x100, 0x101, 0x102} or any(len(d) != 8 for d in data.values()):
        raise ValueError("Expected three standard eight-byte frames")
    if data[0x101][0] not in (0, 1):
        raise ValueError("Invalid brake byte")
    return {"speed_kph": struct.unpack("<H", data[0x100][:2])[0] * .01,
            "brake_pressed": data[0x101][0],
            "steering_deg": struct.unpack("<h", data[0x102][:2])[0] * .1}


class BrakeSwitch:
    def __init__(self, on=5.0, off=2.0):
        if not 0 <= off < on <= 100:
            raise ValueError("Brake thresholds must satisfy 0 <= off < on <= 100")
        self.on, self.off, self.pressed = on, off, False

    def update(self, percent):
        percent = finite(percent)
        if percent >= self.on:
            self.pressed = True
        elif percent <= self.off:
            self.pressed = False
        return self.pressed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Encode/display only (default)")
    mode.add_argument("--send", action="store_true", help="Send using verified CANable2 SLCAN")
    parser.add_argument("--port", help="CANable2 COM port; auto-detected by VID/PID if omitted")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--device-index", type=int)
    speed = parser.add_mutually_exclusive_group()
    speed.add_argument("--virtual-speed-kph", type=float, default=0, help="Constant test speed, independent of throttle")
    speed.add_argument("--virtual-ramp", action="store_true", help="Time-only triangular test speed: 0..60..0 km/h, 40 s period")
    parser.add_argument("--rate-hz", type=float, default=20, help="Cycles per second, three frames per cycle")
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--brake-on-pct", type=float, default=5)
    parser.add_argument("--brake-off-pct", type=float, default=2)
    parser.add_argument("--sample", nargs=3, type=float, metavar=("SPEED_KPH", "BRAKE_0_OR_1", "STEERING_DEG"),
                        help="Offline encoding check; no G29 required")
    args = parser.parse_args()
    if args.sample is not None:
        sample_frames = encode_frames(args.sample[0], args.sample[1] == 1 if args.sample[1] in (0, 1) else 2, args.sample[2])
        for identifier, data in sample_frames:
            print(f"STD 0x{identifier:03X} CLASSIC DLC=8 {data.hex(' ').upper()}")
        print("PC decode preview (not ECU RX):", decode_frames(sample_frames))
        if args.send:
            transport = CanableTransport(args.port)
            try:
                transport.send_frames(sample_frames)
                time.sleep(.1)
                print(f"Submitted {transport.submitted} frames to adapter; verify can_*_rx_count on STM32.")
            finally:
                transport.close()
        return
    if not math.isfinite(args.rate_hz) or not 0 < args.rate_hz <= 100:
        parser.error("--rate-hz must be in (0, 100]")
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds must be finite and nonnegative")
    encode_frames(args.virtual_speed_kph, False, 0)
    switch = BrakeSwitch(args.brake_on_pct, args.brake_off_pct)
    wheel = None
    transport = None
    try:
        # Validate file before opening a device; never use guessed or zeroed calibration.
        config = load_config(args.config)
        wheel = Wheel(args.device_index)
        load_config(args.config, wheel.guid, wheel.count)
        # Settle initial DirectInput state before opening CAN; keep pumping events.
        warmup_end = time.monotonic() + 2
        while time.monotonic() < warmup_end:
            wheel.read()
            wheel.draw(["Waiting for initial G29 input state (2 s); CAN not open yet."])
            time.sleep(.02)
        if args.send:
            transport = CanableTransport(args.port)
        label = "SEND: adapter submissions (not confirmed ECU RX)" if transport else "DRY RUN: no physical CAN transmission"
        print(f"{label}; standard Classic CAN DLC=8, bitrate={BITRATE}", flush=True)
        started = time.monotonic()
        deadline = started
        next_print = started
        cycles = 0
        while not args.seconds or time.monotonic() - started < args.seconds:
            axes, _, _ = wheel.read()
            values = normalize(axes, config)
            elapsed = time.monotonic() - started
            speed_kph = 60 * (1 - abs((elapsed % 40) / 20 - 1)) if args.virtual_ramp else args.virtual_speed_kph
            brake = switch.update(values["brake_pct"])
            frames = encode_frames(speed_kph, brake, values["steering_deg"])
            if transport:
                transport.send_frames(frames)
            cycles += 1
            lines = [label, f"Cycles: {cycles}; virtual speed: {speed_kph:.2f} km/h",
                     f"Steering: {values['steering_deg']:+.1f} deg; brake: {values['brake_pct']:.1f}% -> {int(brake)}",
                     f"Throttle (local only): {values['throttle_pct']:.1f}%"]
            lines += [f"0x{i:03X} DLC=8 {d.hex(' ').upper()}" for i, d in frames]
            parsed = decode_frames(frames)
            lines += [f"PC decode ONLY: speed={parsed['speed_kph']:.2f} brake={parsed['brake_pressed']} steering={parsed['steering_deg']:+.1f}",
                      "Actual ECU parsing: run g29_ecu_monitor.py on ST-LINK COM7"]
            wheel.draw(lines)
            if time.monotonic() >= next_print:
                print(" | ".join(lines[1:]), flush=True)
                next_print = time.monotonic() + 0.2
            deadline += 1 / args.rate_hz
            if deadline < time.monotonic():
                deadline = time.monotonic()
            time.sleep(max(0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped; no further input processed.")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        raise SystemExit(f"Sender stopped: {error}") from error
    finally:
        if transport:
            print(f"Adapter submissions: {transport.submitted}; physical reception requires STM32 counters.")
            transport.close()
        if wheel:
            wheel.close()
        else:
            import sys
            if "pygame" in sys.modules:
                sys.modules["pygame"].quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as error:
        # Includes python-can / pyserial disconnect errors without a stale-input retry.
        raise SystemExit(f"CAN program stopped: {error}") from error
