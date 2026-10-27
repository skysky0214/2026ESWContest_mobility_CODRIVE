"""Linux G29 autocenter owned by the driving controller's lifetime."""
import fcntl
import os
import struct
import math
import time

DEVICE = '/dev/input/by-id/usb-Logitech_G29_Driving_Force_Racing_Wheel-event-joystick'
EV_FF, FF_GAIN, FF_AUTOCENTER = 0x15, 0x60, 0x61

class AutoCenter:
    def __init__(self):
        self.fd = None
        self.settings = None
        self.dynamic = None
        self.last_update = None
        self.strength = 0.

    def _write(self, code, value):
        event = struct.pack('llHHi', 0, 0, EV_FF, code, value)
        if os.write(self.fd, event) != len(event):
            raise OSError('Incomplete force-feedback event write')

    def configure(self, settings):
        self.dynamic = settings if settings.get("speed_sensitive") else None
        enabled = settings.get('enabled', False)
        strength = float(settings.get('strength_percent', 15))
        gain = float(settings.get('gain_percent', 25))
        if not 0 <= strength <= 60 or not 0 <= gain <= 75:
            raise ValueError('Autocenter strength must be 0..60%, gain 0..75%')
        requested = (enabled, strength, gain)
        if requested == self.settings:
            return
        if not enabled:
            self.close()
            self.settings = requested
            return
        try:
            if self.fd is None:
                self.fd = os.open(DEVICE, os.O_RDWR | os.O_NONBLOCK)
                features = bytearray(16)
                fcntl.ioctl(self.fd, 0x80104535, features, True)
                if not features[FF_AUTOCENTER // 8] & (1 << (FF_AUTOCENTER % 8)):
                    raise RuntimeError('G29 driver does not support autocenter')
            self._write(FF_GAIN, round(65535 * gain / 100))
            self._write(FF_AUTOCENTER, round(65535 * strength / 100))
            self.settings = requested
            self.strength = strength
            self.last_update = time.monotonic()
        except Exception:
            self.close()
            raise

    def update_speed(self, speed_mps, fresh=True):
        if self.fd is None or not self.dynamic:return
        now=time.monotonic()
        dt=min(.25,max(0.,now-(self.last_update or now)))
        self.last_update=now
        curve=self.dynamic['speed_curve_kmh']
        speed=max(0.,float(speed_mps))*3.6 if fresh and math.isfinite(speed_mps) else 0.
        target=curve[-1][1]
        for (a,va),(b,vb) in zip(curve,curve[1:]):
            if speed<=b:
                target=va+(vb-va)*max(0.,min(1.,(speed-a)/(b-a)));break
        target=max(0.,min(40.,target))
        step=self.dynamic.get('slew_percent_s',8)*dt
        strength=self.strength+max(-step,min(step,target-self.strength))
        if abs(strength-self.strength)>=.15:
            self._write(FF_AUTOCENTER,round(65535*strength/100))
            self.strength=strength

    def close(self):
        if self.fd is not None:
            try:
                self._write(FF_AUTOCENTER, 0)
                self._write(FF_GAIN, 65535)
            except OSError:
                # Device removal already stops the motor.
                pass
            finally:
                os.close(self.fd)
                self.fd = None
                self.settings = None
