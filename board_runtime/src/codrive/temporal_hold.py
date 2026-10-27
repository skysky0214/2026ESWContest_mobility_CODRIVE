"""Confirm conditions using unique capture times, never repeated UI/evaluation ticks."""


class CaptureHold:
    def __init__(self, hold_ms, maximum_gap_ms):
        self.hold_ns = hold_ms * 1_000_000
        self.gap_ns = maximum_gap_ms * 1_000_000
        self.states = {}

    def clear(self, key=None):
        if key is None: self.states.clear()
        else: self.states.pop(key, None)

    def update(self, key, stamp, active, identity):
        if not active:
            self.clear(key)
            return False
        prior = self.states.get(key)
        if prior is None or prior[0] != identity or not 0 <= stamp-prior[2] <= self.gap_ns:
            since = stamp
        else:
            since = prior[1]
        self.states[key] = identity, since, stamp
        return stamp-since >= self.hold_ns
