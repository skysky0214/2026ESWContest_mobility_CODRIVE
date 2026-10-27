"""Decode actual CAN ECU UART reports; never infer reception from PC transmission."""
from __future__ import annotations
from dataclasses import dataclass

_FIELDS = ('ms', 'speed_raw', 'brake', 'steer_raw', 'rx', 'valid', 'speed_n', 'brake_n',
           'steer_n', 'speed_ms', 'brake_ms', 'steer_ms', 'err', 'invalid', 'lost', 'bo', 'lec')

@dataclass(frozen=True, slots=True)
class EcuTelemetry:
    values: dict[str, int]

    @classmethod
    def parse(cls, line: str) -> 'EcuTelemetry':
        if not line.startswith('ECU,'):
            raise ValueError('Not ECU telemetry')
        pairs = [part.split('=', 1) for part in line.strip().split(',')[1:]]
        if any(len(pair) != 2 for pair in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ValueError('Malformed or duplicate ECU fields')
        fields = dict(pairs)
        values = {key: int(fields[key]) for key in _FIELDS}
        if 'v' in fields:
            if int(fields['v']) != 2:
                raise ValueError('Unsupported ECU version')
            values.update({key: int(fields[key]) for key in ('v', 'accel_raw', 'accel_n', 'accel_ms')})
            if not 0 <= values['accel_raw'] <= 10000:
                raise ValueError('Accelerator out of range')
        if values['brake'] not in (0, 1) or not 0 <= values['speed_raw'] <= 65535 or not -4500 <= values['steer_raw'] <= 4500:
            raise ValueError('ECU signal out of range')
        if any(not 0 <= value <= 0xffffffff for key, value in values.items() if key != 'steer_raw'):
            raise ValueError('Invalid ECU counter/timestamp')
        if values['bo'] not in (0, 1) or not 0 <= values['lec'] <= 7:
            raise ValueError('Invalid ECU bus status')
        return cls(values)

    @property
    def signals(self):
        return ('speed', 'brake', 'steer', 'accel') if self.values.get('v') == 2 else ('speed', 'brake', 'steer')

    @property
    def ages_ms(self) -> tuple[int, ...]:
        return tuple((self.values['ms'] - self.values[key + '_ms']) & 0xffffffff for key in self.signals)

    def valid(self, stale_ms: int = 500) -> bool:
        return (all(self.values[key + '_n'] > 0 for key in self.signals)
                and max(self.ages_ms) <= stale_ms and self.values['bo'] == 0)
        # Firmware reports can_last_protocol_error: a latched historical error,
        # never cleared by successful frames. Keep it diagnostic; freshness and
        # actual per-ID reception determine whether measurements are usable.

    def to_vehicle_state(self, header, *, stale_ms: int = 500):
        from ..models import VehicleState
        return VehicleState(header=header, speed_mps=self.values['speed_raw'] * .01 / 3.6,
                            steering_wheel_deg=self.values['steer_raw'] * .1,
                            brake_pressed=bool(self.values['brake']), accelerator_pct=self.values.get('accel_raw', 0) * .01,
                            gear='unknown', can_valid=self.valid(stale_ms),
                            signal_times_ns=tuple(
                                (name, max(0, header.capture_time_ns - age * 1_000_000))
                                for name, age in zip(('speed_mps', 'brake_pressed', 'steering_wheel_deg', 'accelerator_pct'), self.ages_ms)
                                if self.valid(stale_ms)))
        # The three-frame ECU contract has no accelerator/gear signals.
        # accelerator_pct=0.0 is the existing schema's fallback, not a measured pedal value.
