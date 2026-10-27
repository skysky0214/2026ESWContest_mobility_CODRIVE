"""Confirm cabin behavior across fresh frames before displaying or emitting alerts."""

from dataclasses import dataclass, field

from .models import DriverState


def observed_behavior(driver: DriverState, attention_threshold: float = .35) -> str | None:
    if driver.behavior is not None:
        return "drowsy" if driver.behavior == "head_drop" else driver.behavior
    if driver.phone_detected:
        return "phone"
    if driver.drowsiness_score >= .65:
        return "drowsy"
    if not driver.looking_forward or driver.attention_score < attention_threshold:
        return "distracted"
    return "awake" if driver.attention_score >= .65 else None


@dataclass(slots=True)
class DriverAttentionGate:
    drowsy_ms: int = 2000
    distracted_ms: int = 3000
    recovery_ms: int = 1000
    attention_threshold: float = .35
    active_behavior: str | None = None
    _condition: str | None = None
    _since_ns: int | None = None
    _normal_since_ns: int | None = None
    _normal_seen: bool = False
    _last_now_ns: int | None = None
    _driver: DriverState | None = field(default=None, repr=False)

    def __post_init__(self):
        for value in (self.drowsy_ms, self.distracted_ms, self.recovery_ms):
            if type(value) is not int or value <= 0:
                raise ValueError("driver confirmation and recovery times must be positive milliseconds")
        if not 0 <= self.attention_threshold <= 1:
            raise ValueError("attention threshold must be in [0, 1]")

    def _reset(self):
        self.active_behavior = self._condition = self._since_ns = self._normal_since_ns = None
        self._normal_seen = False
        self._driver = None

    def update(self, driver: DriverState | None, *, now_ns: int):
        gap = self._last_now_ns is not None and not 0 <= now_ns - self._last_now_ns <= 400_000_000
        self._last_now_ns = now_ns
        if gap:
            self._reset()
        if driver is None or not driver.face_detected or not 0 <= now_ns - driver.header.capture_time_ns <= 400_000_000:
            self._reset()
            return
        behavior = observed_behavior(driver, self.attention_threshold)
        if behavior is None:
            self._reset()
            return
        previous = self._driver
        stamp = driver.header.capture_time_ns
        if previous is not None:
            old, new = previous.header, driver.header
            same_source = (old.source_id, old.boot_id, previous.camera_id) == (new.source_id, new.boot_id, driver.camera_id)
            if same_source and old.sequence == new.sequence and old.capture_time_ns == stamp:
                return  # Repainting a cached frame never advances confirmation or recovery.
            if same_source and (new.sequence <= old.sequence or stamp <= old.capture_time_ns):
                self._reset()
                return
            if not same_source or stamp - old.capture_time_ns > 400_000_000:
                self._reset()
        self._driver = driver
        if behavior in {"drowsy", "distracted"}:
            self._normal_since_ns = None
            if self._condition != behavior:
                self._condition, self._since_ns = behavior, stamp
            required = self.drowsy_ms if behavior == "drowsy" else self.distracted_ms
            if stamp - self._since_ns >= required * 1_000_000:
                self.active_behavior = behavior
        else:
            self._condition = self._since_ns = None
            if behavior == "phone":
                self.active_behavior = self._normal_since_ns = None
            else:
                self._normal_seen = True
                if self._normal_since_ns is None:
                    self._normal_since_ns = stamp
                if stamp - self._normal_since_ns >= self.recovery_ms * 1_000_000:
                    self.active_behavior = None

    def status(self) -> dict:
        driver = self._driver
        behavior = observed_behavior(driver, self.attention_threshold) if driver is not None else None
        display = ("phone" if behavior == "phone" else self.active_behavior or
                   ("awake" if self._normal_seen else None))
        elapsed_ms = ((driver.header.capture_time_ns - self._since_ns) / 1e6
                      if driver is not None and self._since_ns is not None else 0)
        return {
            "status": "valid" if driver is not None else "unknown",
            "header": {name: getattr(driver.header, name) for name in (
                "source_id", "boot_id", "sequence", "capture_time_ns")} if driver is not None else None,
            "observed_behavior": behavior, "display_behavior": display,
            "active_behavior": self.active_behavior, "elapsed_ms": elapsed_ms,
            "drowsy_confirm_ms": self.drowsy_ms, "distraction_confirm_ms": self.distracted_ms,
            "recovery_ms": self.recovery_ms,
        }
