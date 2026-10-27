"""Receive-only Panda/DBC input with per-signal timestamps and a latest-value queue."""

from __future__ import annotations

import logging
import math
import queue
import subprocess
import threading
from pathlib import Path

from .models import Header, VehicleState


class DbcVehicleDecoder:
    def __init__(self, config, *, database=None):
        required = config.get("required_signals", {})
        self.signals = {}
        for name in ("speed_mps", "steering_wheel_deg", "brake_pressed", "accelerator_pct"):
            spec = required.get(name)
            if spec is None:
                continue
            if (
                not isinstance(spec, dict)
                or not isinstance(spec.get("message_id"), int)
                or not isinstance(spec.get("signal"), str)
            ):
                raise TypeError(f"{name} requires message_id and signal mapping")
            scale, offset = spec.get("scale", 1), spec.get("offset", 0)
            if not all(
                isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)
                for x in (scale, offset)
            ):
                raise ValueError("invalid DBC conversion")
            self.signals[name] = dict(spec, scale=scale, offset=offset, bus=spec.get("bus", 0))
        if not self.signals:
            raise ValueError("no verified CAN signal mappings configured")
        if database is None:
            import cantools

            path = config.get("dbc_path")
            if not path:
                raise ValueError("DBC path is required")
            database = cantools.database.load_file(path)
        self.database = database
        self.values = {
            "speed_mps": 0.0,
            "steering_wheel_deg": 0.0,
            "brake_pressed": False,
            "accelerator_pct": 0.0,
        }
        self.times = {}

    def decode(self, frames, header):
        changed = False
        for frame in frames:
            specs = [
                (name, spec)
                for name, spec in self.signals.items()
                if frame.arbitration_id == spec["message_id"] and frame.bus == spec["bus"]
            ]
            if not specs:
                continue
            try:
                decoded = self.database.decode_message(
                    frame.arbitration_id, frame.data, decode_choices=False
                )
            except Exception:
                logging.getLogger(__name__).debug("DBC frame decoding failed", exc_info=True)
                continue
            for name, spec in specs:
                raw = decoded.get(spec["signal"])
                if not isinstance(raw, (float, int)) or not math.isfinite(raw):
                    continue
                value = raw * spec["scale"] + spec["offset"]
                if name == "brake_pressed":
                    if value not in (0, 1):
                        continue
                    value = bool(value)
                if name == "speed_mps" and not 0 <= value <= 120:
                    continue
                if name == "accelerator_pct" and not 0 <= value <= 100:
                    continue
                if name == "steering_wheel_deg" and not -2000 <= value <= 2000:
                    continue
                self.values[name] = value
                self.times[name] = header.capture_time_ns
                changed = True
        if not changed:
            return None
        return VehicleState(
            header,
            **self.values,
            signal_times_ns=tuple(sorted(self.times.items())),
            can_valid="speed_mps" in self.times,
        )


class VehicleWorker:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self._latest = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._status = {"status": "waiting_configuration"}

    def status(self):
        with self._lock:
            return dict(self._status)

    def _state(self, **values):
        with self._lock:
            self._status.update(values)

    def start(self):
        import yaml

        try:
            config = yaml.safe_load(self.config_path.read_text())
            if config.get("receive_only") is not True:
                raise ValueError("CAN config must specify receive_only: true")
            if not config.get("dbc_path"):
                raise ValueError("supply a validated DBC path and signal mapping")
            self.decoder = DbcVehicleDecoder(config)
            self.config = config
        except (OSError, ValueError, TypeError, ImportError, AttributeError, yaml.YAMLError) as exc:
            self._state(status="waiting_configuration", error=str(exc))
            return
        self._thread = threading.Thread(target=self._run, name="codrive-can", daemon=True)
        self._thread.start()

    def _run(self):
        from .hardware.panda_can import PandaCanReceiver

        sequence = 0
        while not self._stop.is_set():
            try:
                if self.config.get('interface_type')=='socketcan':
                    from .hardware.socketcan import SocketCanReceiver
                    connection=SocketCanReceiver(self.config.get('channel','can0'))
                else:
                    connection=PandaCanReceiver.connect(self.config.get("usb_serial"),
                        allowed_buses=self.config.get("expected_buses", [0]))
                with connection as receiver:
                    self._state(status="receiving", error=None)
                    while not self._stop.is_set():
                        frames = receiver.read_available()
                        if frames:
                            sequence += 1
                            sample = self.decoder.decode(frames, Header.now("pi5.socketcan" if self.config.get("interface_type")=="socketcan" else "pi5.panda", sequence))
                            if sample is not None:
                                try:
                                    self._latest.get_nowait()
                                except queue.Empty:
                                    pass
                                self._latest.put_nowait(sample)
                        self._stop.wait(0.01)
            except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                # A reconnect must not reuse a previous connection's signal history.
                self.decoder.times.clear()
                self._state(status="disconnected", error=str(exc)[:240])
                self._stop.wait(30)

    def take_latest(self):
        try:
            return self._latest.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
