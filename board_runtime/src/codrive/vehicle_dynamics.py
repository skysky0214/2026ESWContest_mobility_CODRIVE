"""Short-window acceleration estimate from unique, fresh CAN speed observations."""
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SpeedAcceleration:
    window_ms: int = 500
    minimum_span_ms: int = 300
    maximum_gap_ms: int = 250
    samples: deque = field(default_factory=deque)
    identity: tuple | None = None
    acceleration_mps2: float | None = None

    def clear(self):
        self.samples.clear()
        self.identity = None
        self.acceleration_mps2 = None

    def observe(self, vehicle, now_ns):
        if (vehicle is None or not vehicle.can_valid or vehicle.header.source_id != 'pi5.can_ecu'
                or not -5_000_000 <= now_ns-vehicle.header.capture_time_ns <= self.maximum_gap_ms*1_000_000):
            self.clear()
            return None
        h = vehicle.header
        identity = (h.source_id, h.boot_id, h.sequence)
        if identity == self.identity:
            return self.acceleration_mps2
        if self.identity is not None and (identity[:2] != self.identity[:2] or identity[2] <= self.identity[2]):
            self.clear()
        stamp = h.capture_time_ns
        if self.samples and not 0 < stamp-self.samples[-1][0] <= self.maximum_gap_ms*1_000_000:
            self.clear()
        if self.samples and abs(vehicle.speed_mps-self.samples[-1][1])*1e9/(stamp-self.samples[-1][0]) > 15:
            self.clear()
        self.identity = identity
        self.samples.append((stamp, vehicle.speed_mps))
        while len(self.samples) > 1 and stamp-self.samples[0][0] > self.window_ms*1_000_000:
            self.samples.popleft()
        self.acceleration_mps2 = None
        if len(self.samples) < 4 or stamp-self.samples[0][0] < self.minimum_span_ms*1_000_000:
            return None
        times = [(item[0]-stamp)/1e9 for item in self.samples]
        speeds = [item[1] for item in self.samples]
        mean_t, mean_v = sum(times)/len(times), sum(speeds)/len(speeds)
        slope = sum((t-mean_t)*(v-mean_v) for t, v in zip(times, speeds))/sum((t-mean_t)**2 for t in times)
        # Discontinuous respawns/clock changes must warm up again instead of warning.
        if abs(slope) > 15:
            self.clear()
            self.identity = identity
            self.samples.append((stamp, vehicle.speed_mps))
            return None
        self.acceleration_mps2 = slope
        return slope

    def status(self):
        return {'source': 'carla_speed_over_can', 'acceleration_mps2': self.acceleration_mps2,
                'sample_count': len(self.samples),
                'span_ms': (self.samples[-1][0]-self.samples[0][0])/1e6 if self.samples else 0,
                'valid': self.acceleration_mps2 is not None}
