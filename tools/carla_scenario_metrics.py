"""CARLA reference measurements only; never publishes perception or risk inputs."""
from collections import deque
import math


class TrialMetrics:
    def __init__(self):
        self.speeds = deque()
        self.observed = set()
        self.acceleration = None

    def measure(self, stamp, speed_mps):
        if not math.isfinite(stamp) or not math.isfinite(speed_mps):
            self.speeds.clear()
            self.acceleration = None
            return None
        if self.speeds:
            dt = stamp - self.speeds[-1][0]
            if dt == 0:
                return self.acceleration
            if dt < 0 or dt > .25 or abs(speed_mps-self.speeds[-1][1])/dt > 15:
                self.speeds.clear()
        self.speeds.append((stamp, speed_mps))
        while len(self.speeds) > 1 and stamp-self.speeds[0][0] > .5:
            self.speeds.popleft()
        self.acceleration = None
        if len(self.speeds) >= 4 and stamp-self.speeds[0][0] >= .3:
            times = [t-stamp for t, _ in self.speeds]
            values = [v for _, v in self.speeds]
            mt, mv = sum(times)/len(times), sum(values)/len(values)
            self.acceleration = sum((t-mt)*(v-mv) for t, v in zip(times, values))/sum((t-mt)**2 for t in times)
        return self.acceleration

    def observe(self, config, row):
        """Return newly observed reference events, not judgment-core verdicts."""
        a = self.measure(row['carla_elapsed_s'], row['ego_speed_kmh']/3.6)
        row['reference_acceleration_mps2'] = a
        row['reference_left_lane_warning'] = row['ego_route_right_m'] <= -config.get('lane_warning_offset_m', .65)
        row['reference_rapid_acceleration'] = (a is not None and a >= config.get('acceleration_threshold_mps2', 4)
            and row['control']['throttle'] >= config.get('accelerator_threshold', .9)
            and row['control']['brake'] <= .01)
        active = []
        kind = config.get('kind', 'pedestrian')
        if kind == 'pedestrian_lane' and row['state'] == 'crossing' and row['ego_speed_kmh'] >= 5:
            if row['reference_left_lane_warning']:
                active.append('reference_compound_window')
        if kind == 'rapid_acceleration' and row['reference_rapid_acceleration']:
            active.append('reference_rapid_acceleration')
        new = [name for name in active if name not in self.observed]
        self.observed.update(new)
        return new
