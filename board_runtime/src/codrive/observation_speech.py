"""Confirm cabin INFO observations and bound repeated speech by elapsed time.

Missing/no-face input cannot trigger speech; no awake classification is required.
WARNING/CRITICAL decisions continue through the existing arbiter unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import DriverState, HmiCommand, Severity

OBSERVATION_CODES = frozenset({
    "driver_phone_observation", "driver_drowsy_observation", "driver_attention_observation",
})


@dataclass(slots=True)
class ObservationSpeechGate:
    confirm_ms: int = 2_000
    repeat_ms: int = 10_000
    _code: str | None = None
    _since_ns: int | None = None
    _last_update_ns: int | None = None
    _spoken_at: dict[str, int] = field(default_factory=dict)

    def update(self, driver: DriverState | None, *, now_ns: int) -> None:
        gap = self._last_update_ns is not None and not 0 <= now_ns - self._last_update_ns <= 400_000_000
        self._last_update_ns = now_ns
        valid = driver is not None and driver.face_detected and -5_000_000 <= now_ns - driver.header.capture_time_ns <= 400_000_000
        if gap or not valid:
            self._code = self._since_ns = None
        if not valid:
            return
        if driver.behavior == "phone" or (driver.behavior is None and driver.phone_detected):
            code = "driver_phone_observation"
        elif driver.behavior in {"drowsy", "head_drop"} or (driver.behavior is None and driver.drowsiness_score >= .65):
            code = "driver_drowsy_observation"
        elif driver.behavior == "distracted" or (driver.behavior is None and not driver.looking_forward and driver.attention_score < .65):
            code = "driver_attention_observation"
        else:
            code = None
        if code is None:
            self._code = self._since_ns = None
        else:
            if code != self._code:
                # Share the capture-time basis with driver confirmation; varying
                # transport delay must not add a second wait after qualification.
                self._code, self._since_ns = code, driver.header.capture_time_ns

    def eligible(self, command: HmiCommand, *, now_ns: int) -> bool:
        if command.severity is not Severity.INFO or command.risk_code not in OBSERVATION_CODES:
            return True
        last_spoken = self._spoken_at.get(command.risk_code)
        return (command.risk_code == self._code
                and self._since_ns is not None
                and now_ns - self._since_ns >= self.confirm_ms * 1_000_000
                and (last_spoken is None or now_ns - last_spoken >= self.repeat_ms * 1_000_000))

    def mark_spoken(self, command: HmiCommand) -> None:
        if command.severity is Severity.INFO and command.risk_code in OBSERVATION_CODES:
            self._spoken_at[command.risk_code] = command.header.capture_time_ns
