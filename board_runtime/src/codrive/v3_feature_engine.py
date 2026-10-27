"""Bounded local rules on final sensor outputs; no network or inference here."""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .models import AudioKind, Header, RiskEvent, Severity, SituationSnapshot, TrafficLight, Weather
from .sensor_features import build_inputs, environment_validity, summarize_objects


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    minimum_motion_mps: float = 1.4
    distraction_threshold: float = 0.35
    duplicate_cooldown_ms: int = 900
    event_ttl_ms: int = 1500
    cabin_observation_enabled: bool = False
    audio_alerts_enabled: bool = True
    vehicle_ttl_ms: int = 120
    external_ttl_ms: int = 250
    driver_ttl_ms: int = 400
    audio_ttl_ms: int = 1000
    environment_ttl_ms: int = 300000
    context_skew_ms: int = 200
    driver_warning_ms: int = 1500
    driver_critical_ms: int = 4000
    road_camera_ids: tuple[str, ...] = ("front",)
    night_weight: float = 1.15
    rain_weight: float = 1.20
    snow_weight: float = 1.35
    fog_weight: float = 1.40
    thunder_weight: float = 1.35
    visibility_warning_m: float = 1000
    visibility_critical_m: float = 200
    visibility_max_weight: float = 1.6
    environment_weight_cap: float = 2.0
    acceleration_threshold_mps2: float = 3.0
    acceleration_hold_ms: int = 300
    pedal_rate_pct_s: float = 300
    pedal_min_change_pct: float = 20
    pedal_min_pct: float = 60
    pedal_hold_ms: int = 100
    motion_window_ms: int = 200
    motion_max_gap_ms: int = 150
    object_center_min: float = 0.25
    object_center_max: float = 0.75

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {"cabin_observation_enabled", "audio_alerts_enabled"}:
                if type(value) is not bool:
                    raise ValueError(f"{item.name} must be boolean")
                continue
            if item.name == "road_camera_ids":
                if (
                    not isinstance(value, (tuple, list))
                    or not value
                    or any(not isinstance(x, str) or not x for x in value)
                ):
                    raise ValueError("road_camera_ids must contain camera IDs")
                continue
            if isinstance(value, bool):
                raise TypeError(f"invalid boolean risk setting: {item.name}")
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid risk setting: {item.name}")
        if any(
            getattr(self, name) <= 0
            for name in (
                "vehicle_ttl_ms",
                "external_ttl_ms",
                "driver_ttl_ms",
                "audio_ttl_ms",
                "environment_ttl_ms",
                "event_ttl_ms",
                "acceleration_threshold_mps2",
                "pedal_rate_pct_s",
            )
        ):
            raise ValueError("risk TTLs and motion thresholds must be positive")
        if self.motion_window_ms <= 0 or self.motion_max_gap_ms <= 0:
            raise ValueError("motion timing must be positive")
        if not 0 <= self.object_center_min < self.object_center_max <= 1:
            raise ValueError("invalid object center bounds")
        if not 0 < self.visibility_critical_m < self.visibility_warning_m:
            raise ValueError("invalid visibility bounds")
        for name in (
            "night_weight",
            "rain_weight",
            "snow_weight",
            "fog_weight",
            "thunder_weight",
            "visibility_max_weight",
            "environment_weight_cap",
        ):
            if getattr(self, name) < 1:
                raise ValueError("environment weights must be at least one")
        if self.driver_critical_ms < self.driver_warning_ms:
            raise ValueError("driver duration thresholds must be increasing")


def load_risk_policy(path: str | Path, **overrides) -> RiskPolicy:
    import yaml

    data = yaml.safe_load(Path(path).read_text())
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("risk"), dict)
    ):
        raise ValueError("risk config requires schema_version: 1 and risk mapping")
    values = dict(data["risk"])
    if set(values) - {item.name for item in fields(RiskPolicy)}:
        raise ValueError("unknown risk settings")
    if "road_camera_ids" in values:
        values["road_camera_ids"] = tuple(values["road_camera_ids"])
    values.update(overrides)
    return RiskPolicy(**values)


@dataclass(frozen=True, slots=True)
class _Candidate:
    code: str
    severity: Severity
    score: float
    action: str
    message_key: str
    evidence: tuple[str, ...]
    direction: str = "unknown"


@dataclass(slots=True)
class RiskEngine:
    source_id: str = "pi5.risk"
    policy: RiskPolicy = field(default_factory=RiskPolicy)
    _sequence: int = 0
    _last_emitted_ns: dict[tuple[str, str], int] = field(default_factory=dict)
    _last_severity: dict[tuple[str, str], Severity] = field(default_factory=dict)
    _last_selected: tuple[str, str] | None = None
    _driver_state: tuple | None = None
    _motion_identity: tuple | None = None
    _motion: dict = field(default_factory=dict)
    _holds: dict = field(default_factory=dict)
    _last_now: int | None = None
    _environment_key: tuple | None = None
    _environment_cache: tuple = (1.0, ())
    last_candidates: tuple[_Candidate, ...] = ()
    unavailable_inputs: tuple[str, ...] = ()
    last_features: dict = field(default_factory=dict)
    _motion_features: dict = field(default_factory=dict)

    @staticmethod
    def _fresh(value, now, ttl):
        return value is not None and 0 <= now - value.header.capture_time_ns <= ttl * 1000000

    def _aligned(self, *values):
        if any(value is None for value in values):
            return False
        stamps = [value.header.capture_time_ns for value in values]
        return max(stamps) - min(stamps) <= self.policy.context_skew_ms * 1000000

    def _inputs(self, snapshot, now):
        unavailable = []
        values = {}
        for name in ("vehicle", "driver", "audio", "environment"):
            value = getattr(snapshot, name)
            if not self._fresh(value, now, getattr(self.policy, name + "_ttl_ms")):
                unavailable.append(name)
                value = None
            values[name] = value
        vehicle = values["vehicle"]
        if vehicle is not None:
            stamps = dict(vehicle.signal_times_ns)
            speed_verified = not stamps or (
                "speed_mps" in stamps
                and 0 <= now - stamps["speed_mps"] <= self.policy.vehicle_ttl_ms * 1000000
            )
            if not vehicle.can_valid or not speed_verified:
                unavailable.append("vehicle:invalid_or_stale_speed")
                values["vehicle"] = None
        values["external"] = tuple(
            x for x in snapshot.external if self._fresh(x, now, self.policy.external_ttl_ms)
        )
        if not values["external"]:
            unavailable.append("external")
        self.unavailable_inputs = tuple(unavailable)
        return replace(snapshot, **values)

    def _driver(self, driver):
        if driver is None or not driver.face_detected:
            self._driver_state = None
            return None, 0
        behavior = driver.behavior
        if behavior is None:
            behavior = (
                "phone"
                if driver.phone_detected
                else "drowsy"
                if driver.drowsiness_score >= 0.65
                else "distracted"
                if not driver.looking_forward and driver.attention_score < 0.65
                else "awake"
            )
        now = driver.header.capture_time_ns
        identity = (driver.header.source_id, driver.header.boot_id, behavior)
        prior = self._driver_state
        since = now
        if (
            prior
            and prior[0] == identity
            and 0 <= now - prior[2] <= self.policy.driver_ttl_ms * 1000000
        ):
            since = prior[1]
        self._driver_state = identity, since, now
        return behavior, (now - since) / 1000000

    def _environment(self, env, now):
        if env is None:
            return 1.0, ()
        weather_valid, daylight_valid = environment_validity(env, now)
        key = (
            env.weather,
            env.is_night,
            env.precipitation_mm_h,
            env.snowfall_cm_h,
            env.visibility_m,
            weather_valid,
            daylight_valid,
        )
        if key == self._environment_key:
            return self._environment_cache
        visibility, surface, evidence = 1.0, 1.0, []
        if daylight_valid and env.is_night:
            visibility = self.policy.night_weight
            evidence.append("night=true")
        if weather_valid:
            if env.weather is Weather.FOG:
                visibility = max(visibility, self.policy.fog_weight)
            if env.visibility_m is not None and env.visibility_m < self.policy.visibility_warning_m:
                fraction = min(
                    1,
                    max(
                        0,
                        (self.policy.visibility_warning_m - env.visibility_m)
                        / (self.policy.visibility_warning_m - self.policy.visibility_critical_m),
                    ),
                )
                visibility = max(visibility, 1 + fraction * (self.policy.visibility_max_weight - 1))
            if env.weather is Weather.RAIN or env.precipitation_mm_h > 0:
                surface = self.policy.rain_weight
            if env.weather is Weather.SNOW or env.snowfall_cm_h > 0:
                surface = max(surface, self.policy.snow_weight)
            if env.weather is Weather.THUNDER:
                surface = max(surface, self.policy.thunder_weight)
            evidence.append(f"weather={env.weather.value}")
        weight = min(self.policy.environment_weight_cap, visibility * surface)
        self._environment_key = key
        self._environment_cache = weight, tuple(evidence)
        return self._environment_cache

    def _road(self, snapshot, behavior, weight, env_evidence):
        candidates = []
        vehicle = snapshot.vehicle
        moving = vehicle is not None and vehicle.speed_mps >= self.policy.minimum_motion_mps
        impaired = behavior in {"phone", "drowsy", "head_drop", "distracted"}
        for perception in snapshot.external:
            if perception.camera_id not in self.policy.road_camera_ids:
                continue
            paired = self._aligned(vehicle, perception)
            driver_paired = impaired and self._aligned(snapshot.driver, perception)
            counts, stop_sign, largest_box, central = summarize_objects(perception, self.policy)
            count = sum(counts.values())
            if count and (vehicle is None or moving):
                severity = (
                    Severity.WARNING
                    if paired and moving and driver_paired and central
                    else Severity.INFO
                )
                # Image boxes establish presence/attention, never distance or collision imminence.
                score = (205 if severity is Severity.WARNING else 125) + min(count, 5) * 2
                score = (score + min(largest_box, 0.5) * 10) * weight
                if paired and moving and score >= 200:
                    severity = Severity.WARNING
                candidates.append(
                    _Candidate(
                        "road_user_attention",
                        severity,
                        score,
                        "check_surroundings",
                        "road_users",
                        tuple(f"{k}_count={v}" for k, v in counts.items())
                        + (
                            f"central_image_region={central}",
                            f"max_bbox_area={largest_box:.3f}",
                            f"environment_weight={weight:.2f}",
                            *env_evidence,
                        ),
                    )
                )
            if stop_sign and paired and moving:
                candidates.append(
                    _Candidate(
                        "stop_sign_attention",
                        Severity.INFO,
                        140 * weight,
                        "check_surroundings",
                        "stop_sign",
                        ("stop_sign_detected", *env_evidence),
                    )
                )
            brake_stamps = dict(vehicle.signal_times_ns) if vehicle is not None else {}
            brake_valid = not brake_stamps or (
                "brake_pressed" in brake_stamps
                and 0
                <= snapshot.header.capture_time_ns - brake_stamps["brake_pressed"]
                <= self.policy.vehicle_ttl_ms * 1000000
            )
            if not paired or not moving or not brake_valid or vehicle.brake_pressed:
                continue
            light = perception.traffic_light
            if light is TrafficLight.RED:
                severity = Severity.CRITICAL if vehicle.speed_mps >= 8.3 else Severity.WARNING
                score, code, message = (
                    290 + vehicle.speed_mps,
                    "red_light_approach",
                    "red_light_brake",
                )
            elif light is TrafficLight.YELLOW:
                severity, score, code, message = (
                    Severity.WARNING,
                    215 + vehicle.speed_mps,
                    "yellow_light_approach",
                    "yellow_light_slow",
                )
            else:
                continue
            score *= weight * (1.2 if driver_paired else 1)
            if score >= 300:
                severity = Severity.CRITICAL
            candidates.append(
                _Candidate(
                    code,
                    severity,
                    score,
                    "brake_now" if light is TrafficLight.RED else "slow_down",
                    message,
                    (
                        f"speed_mps={vehicle.speed_mps:.2f}",
                        "brake=false",
                        f"driver_context={behavior}",
                        f"environment_weight={weight:.2f}",
                        *env_evidence,
                    ),
                )
            )
        return candidates

    def _driver_audio(self, snapshot, behavior, duration):
        candidates = []
        driver, vehicle = snapshot.driver, snapshot.vehicle
        moving = (
            self._aligned(driver, vehicle) and vehicle.speed_mps >= self.policy.minimum_motion_mps
        )
        observations = {
            "phone": ("driver_phone_observation", "phone_observation", 115),
            "drowsy": ("driver_drowsy_observation", "drowsy_observation", 110),
            "head_drop": ("driver_drowsy_observation", "head_drop_observation", 112),
            "distracted": ("driver_attention_observation", "attention_observation", 105),
        }
        if behavior in observations:
            code, message, score = observations[behavior]
            severity = Severity.INFO
            if moving:
                code = {
                    "phone": "driver_phone_use",
                    "drowsy": "driver_drowsiness",
                    "head_drop": "driver_head_drop",
                    "distracted": "driver_inattention",
                }[behavior]
                message = {
                    "phone": "phone_observation",
                    "drowsy": "drowsy_observation",
                    "head_drop": "head_drop_observation",
                    "distracted": "look_forward",
                }[behavior]
                severity = (
                    Severity.CRITICAL
                    if duration >= self.policy.driver_critical_ms
                    else Severity.WARNING
                )
                score = 200 + (30 if behavior in {"drowsy", "head_drop"} else 0)
                score += 20 * (
                    driver.drowsiness_score
                    if behavior in {"drowsy", "head_drop"}
                    else 1 - driver.attention_score
                )
                score += min(duration / max(1, self.policy.driver_warning_ms), 2) * 10
                if severity is Severity.CRITICAL:
                    score += 100
            if moving or self.policy.cabin_observation_enabled:
                candidates.append(
                    _Candidate(
                        code,
                        severity,
                        score,
                        "look_forward",
                        message,
                        (
                            f"behavior={behavior}",
                            f"duration_ms={duration:.0f}",
                            f"drowsiness_score={driver.drowsiness_score:.2f}",
                            f"attention={driver.attention_score:.2f}",
                        ),
                    )
                )
        audio = snapshot.audio
        if (
            self.policy.audio_alerts_enabled
            and audio
            and audio.kind in {AudioKind.SIREN, AudioKind.HORN}
        ):
            direction = audio.direction.lower()
            if direction not in {"left", "right"}:
                direction = "unknown"
            siren = audio.kind is AudioKind.SIREN
            severity = Severity.WARNING if siren else Severity.INFO
            score = 225 if siren else 135
            yellow = any(
                self._aligned(audio, x) and x.traffic_light is TrafficLight.YELLOW
                for x in snapshot.external
            )
            if yellow and siren:
                score += 10
            if (
                self._aligned(audio, vehicle)
                and vehicle.speed_mps >= self.policy.minimum_motion_mps
            ):
                if not siren:
                    severity, score = Severity.WARNING, 205
                if behavior in observations and self._aligned(audio, driver):
                    score += 20
            message = ("siren_" if siren else "horn_") + direction
            candidates.append(
                _Candidate(
                    "emergency_vehicle" if siren else "horn_alert",
                    severity,
                    score,
                    "yield_safely" if siren else "check_surroundings",
                    message,
                    (f"direction={direction}", f"yellow_light={yellow}"),
                    direction,
                )
            )
        return candidates

    def _motion_rules(self, vehicle, now, weight):
        if vehicle is None or not vehicle.signal_times_ns:
            self._motion.clear()
            self._holds.clear()
            self._motion_features.clear()
            self._motion_identity = None
            return []
        identity = vehicle.header.source_id, vehicle.header.boot_id
        if identity != self._motion_identity:
            self._motion.clear()
            self._holds.clear()
            self._motion_features.clear()
            self._motion_identity = identity
        stamps = dict(vehicle.signal_times_ns)
        out = []
        specs = (
            (
                "speed_mps",
                "rapid_acceleration",
                self.policy.acceleration_threshold_mps2,
                self.policy.acceleration_hold_ms,
                "rapid_acceleration",
            ),
            (
                "accelerator_pct",
                "rapid_pedal",
                self.policy.pedal_rate_pct_s,
                self.policy.pedal_hold_ms,
                "rapid_pedal",
            ),
        )
        for name, code, threshold, hold, message in specs:
            rate_feature, duration_feature = (
                ("speed_accel_mps2", "acceleration_duration_ms")
                if name == "speed_mps"
                else ("accelerator_rate_pct_s", "pedal_rate_duration_ms")
            )
            timestamp = stamps.get(name)
            if (
                timestamp is None
                or not 0 <= now - timestamp <= self.policy.vehicle_ttl_ms * 1000000
            ):
                self._motion.pop(name, None)
                self._holds.pop(name, None)
                self._motion_features.pop(rate_feature, None)
                self._motion_features.pop(duration_feature, None)
                continue
            history = self._motion.setdefault(name, deque(maxlen=32))
            value = getattr(vehicle, name)
            if history and timestamp <= history[-1][0]:
                if timestamp < history[-1][0]:
                    history.clear()
                    self._holds.pop(name, None)
                    self._motion_features.pop(rate_feature, None)
                    self._motion_features.pop(duration_feature, None)
                else:
                    held = self._holds.get(name)
                    if held and held[2]:
                        out.append(held[2])
                    continue
            if history and timestamp - history[-1][0] > self.policy.motion_max_gap_ms * 1000000:
                history.clear()
                self._holds.pop(name, None)
                self._motion_features.pop(rate_feature, None)
                self._motion_features.pop(duration_feature, None)
            history.append((timestamp, value))
            cutoff = timestamp - self.policy.motion_window_ms * 1000000
            while len(history) > 2 and history[1][0] <= cutoff:
                history.popleft()
            elapsed = (timestamp - history[0][0]) / 1e9
            if elapsed < 0.02:
                continue
            change = value - history[0][1]
            rate = change / elapsed
            self._motion_features[rate_feature] = rate
            self._motion_features[duration_feature] = 0.0
            active = rate >= threshold
            if name == "accelerator_pct":
                active &= (
                    value >= self.policy.pedal_min_pct
                    and change >= self.policy.pedal_min_change_pct
                )
            if not active:
                self._holds.pop(name, None)
                continue
            since = self._holds.get(name, (timestamp, None, None))[0]
            self._motion_features[duration_feature] = (timestamp - since) / 1e6
            candidate = None
            if timestamp - since >= hold * 1000000:
                candidate = _Candidate(
                    code,
                    Severity.WARNING,
                    (230 if name == "speed_mps" else 210) * weight,
                    "ease_accelerator",
                    message,
                    (
                        f"{name}_rate={rate:.2f}",
                        f"observed_ms={(timestamp - since) / 1e6:.0f}",
                        f"environment_weight={weight:.2f}",
                    ),
                )
                out.append(candidate)
            self._holds[name] = since, timestamp, candidate
        return out

    def evaluate(
        self, snapshot: SituationSnapshot, *, now_ns: int | None = None
    ) -> RiskEvent | None:
        now = time.time_ns() if now_ns is None else now_ns
        if self._last_now is not None and now < self._last_now:
            self._driver_state = None
            self._motion.clear()
            self._holds.clear()
            self._motion_features.clear()
            self._last_emitted_ns.clear()
            self._last_severity.clear()
            self._last_selected = None
        self._last_now = now
        snapshot = self._inputs(snapshot, now)
        behavior, duration = self._driver(snapshot.driver)
        weight, evidence = self._environment(snapshot.environment, now)
        candidates = self._road(snapshot, behavior, weight, evidence) + self._driver_audio(
            snapshot, behavior, duration
        )
        candidates += self._motion_rules(snapshot.vehicle, now, weight)
        self.last_features = build_inputs(
            snapshot,
            now=now,
            policy=self.policy,
            behavior=behavior,
            duration=duration,
            motion=self._motion_features,
        )
        self.last_candidates = tuple(
            sorted(candidates, key=lambda c: (c.severity, c.score, c.code), reverse=True)
        )
        if not self.last_candidates:
            self._last_selected = None
            return None
        selected = self.last_candidates[0]
        key = selected.code, selected.direction
        previous = self._last_emitted_ns.get(key)
        changed = key != self._last_selected
        self._last_selected = key
        if (
            previous is not None
            and not changed
            and now - previous < self.policy.duplicate_cooldown_ms * 1000000
            and selected.severity <= self._last_severity.get(key, Severity.INFO)
        ):
            return None
        cutoff = now - max(self.policy.event_ttl_ms, self.policy.duplicate_cooldown_ms) * 1000000
        for old in list(self._last_emitted_ns):
            if self._last_emitted_ns[old] < cutoff:
                del self._last_emitted_ns[old]
                self._last_severity.pop(old, None)
        self._last_emitted_ns[key] = now
        self._last_severity[key] = selected.severity
        self._sequence += 1
        return RiskEvent(
            Header.now(
                self.source_id,
                self._sequence,
                frame_id="vehicle",
                capture_time_ns=now,
                trace_id=snapshot.header.trace_id,
            ),
            uuid.uuid4().hex,
            selected.code,
            selected.severity,
            selected.score,
            selected.action,
            selected.message_key,
            selected.evidence,
            now + self.policy.event_ttl_ms * 1000000,
        )
