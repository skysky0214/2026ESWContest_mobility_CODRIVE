"""Hourly weather and daily solar cache; all HTTP runs in one background worker."""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from .models import EnvironmentState, Header, Weather


@dataclass(frozen=True)
class EnvironmentPolicy:
    enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = "Asia/Seoul"
    weather_interval_s: int = 3600
    weather_max_age_s: int = 4500
    timeout_s: float = 8
    retry_s: int = 60
    cache_path: str = "artifacts/cache/environment.json"
    location_radius_km: float = 20

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        if self.latitude is not None:
            if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
                raise ValueError("invalid latitude")
            if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
                raise ValueError("invalid longitude")
        if self.weather_interval_s != 3600:
            raise ValueError("weather polling interval is fixed at one hour")
        if (
            self.weather_max_age_s < self.weather_interval_s
            or self.retry_s < 1
            or self.timeout_s <= 0
        ):
            raise ValueError("invalid environment timing")


def weather_kind(code):
    if code == 0:
        return Weather.CLEAR
    if code in {1, 2, 3}:
        return Weather.CLOUDY
    if code in {45, 48}:
        return Weather.FOG
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return Weather.RAIN
    if code in {71, 73, 75, 77, 85, 86}:
        return Weather.SNOW
    if code in {95, 96, 99}:
        return Weather.THUNDER
    return Weather.UNKNOWN


def fetch_json(params, timeout):
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    with urlopen(url, timeout=timeout) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("environment response too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("invalid weather response")
    return data


class EnvironmentWorker:
    def __init__(
        self, policy=None, *, fetch=fetch_json, wall_clock=time.time, monotonic=time.monotonic
    ):
        self.policy = policy or EnvironmentPolicy()
        self.fetch, self.wall_clock, self.monotonic = fetch, wall_clock, monotonic
        self._timezone = ZoneInfo(self.policy.timezone)
        self._lock = threading.Lock()
        self._position = (
            None
            if self.policy.latitude is None
            else (self.policy.latitude, self.policy.longitude, None)
        )
        self._weather = None
        self._sun = None
        self._next_weather = 0.0
        self._retry_after = 0.0
        self._failures = 0
        self._sequence = 0
        self._latest = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None
        self._status = {
            "status": "disabled" if not self.policy.enabled else "waiting_location",
            "weather_interval_s": 3600,
            "solar_interval": "daily",
            "error": None,
        }
        self._load_cache()

    def _day(self, epoch):
        return datetime.fromtimestamp(epoch, self._timezone).date().isoformat()

    def _load_cache(self):
        try:
            data = json.loads(Path(self.policy.cache_path).read_text())
            if data.get("schema_version") != 1 or data.get("timezone") != self.policy.timezone:
                return
            # Parsed again before use; disk data never bypasses numeric/location/expiry checks.
            self._weather = data.get("weather")
            self._sun = data.get("sun")
            if self._weather:
                weather = self._weather
                for name in ("received", "source", "until", "precipitation", "snowfall"):
                    self._number(weather[name], minimum=0)
                for name in ("latitude", "longitude"):
                    self._number(weather[name])
                if not -90 <= weather["latitude"] <= 90 or not -180 <= weather["longitude"] <= 180:
                    raise ValueError("invalid cached position")
                if (
                    type(weather["code"]) is not int
                    or weather_kind(weather["code"]) is Weather.UNKNOWN
                ):
                    raise ValueError("invalid cached weather")
                if not (
                    weather["source"] <= weather["received"] <= self.wall_clock()
                    and weather["source"]
                    < weather["until"]
                    <= min(
                        weather["received"] + self.policy.weather_max_age_s,
                        weather["source"] + 5400,
                    )
                ):
                    raise ValueError("invalid cached times")
                remaining = weather["received"] + 3600 - self.wall_clock()
                self._next_weather = self.monotonic() + max(0, remaining)
        except (OSError, ValueError, TypeError, KeyError):
            self._weather = self._sun = None

    def _save_cache(self):
        path = Path(self.policy.cache_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "timezone": self.policy.timezone,
                        "weather": self._weather,
                        "sun": self._sun,
                    },
                    allow_nan=False,
                )
            )
            os.replace(temp, path)
        except OSError as exc:
            self._set_status(error=f"cache: {exc}")

    def _set_status(self, **values):
        with self._lock:
            self._status.update(values)

    def status(self):
        with self._lock:
            return dict(self._status)

    def update_position(self, ins, now_ns):
        if (
            ins is not None
            and ins.fix_quality > 0
            and ins.latitude_deg is not None
            and ins.longitude_deg is not None
            and 0 <= now_ns - ins.header.capture_time_ns <= 500000000
        ):
            with self._lock:
                self._position = (
                    ins.latitude_deg,
                    ins.longitude_deg,
                    ins.header.capture_time_ns / 1e9,
                )

    def _location(self, epoch):
        with self._lock:
            position = self._position
        # Explicit configured location is stable; an expired GNSS fix is not.
        if position is None:
            return None
        if position[2] is not None and not 0 <= epoch - position[2] <= 10:
            return None
        return position[:2]

    def _near(self, record, position):
        try:
            lat, lon = record["latitude"], record["longitude"]
            if not all(math.isfinite(x) for x in (lat, lon, *position)):
                return False
            dlat, dlon = math.radians(lat - position[0]), math.radians(lon - position[1])
            a = (
                math.sin(dlat / 2) ** 2
                + math.cos(math.radians(lat))
                * math.cos(math.radians(position[0]))
                * math.sin(dlon / 2) ** 2
            )
            return 12742 * math.asin(math.sqrt(min(1, a))) <= self.policy.location_radius_km
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def _number(value, *, minimum=None):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("non-finite environment value")
        if minimum is not None and value < minimum:
            raise ValueError("negative environment value")
        return float(value)

    def _parse_weather(self, data, epoch, position):
        current = data["current"]
        source = self._number(current["time"], minimum=0)
        if not 0 <= epoch - source <= 5400:
            raise ValueError("weather observation outside validity window")
        interval = self._number(current.get("interval", 3600), minimum=1)
        code = current["weather_code"]
        if type(code) is not int or weather_kind(code) is Weather.UNKNOWN:
            raise ValueError("unrecognized weather code")
        visibility = current.get("visibility")
        if visibility is not None:
            visibility = self._number(visibility, minimum=0)
        temperature = current.get("temperature_2m")
        if temperature is not None:
            temperature = self._number(temperature)
        return {
            "received": epoch,
            "source": source,
            "until": min(epoch + self.policy.weather_max_age_s, source + 5400),
            "latitude": position[0],
            "longitude": position[1],
            "code": code,
            "precipitation": self._number(current["precipitation"], minimum=0) * 3600 / interval,
            "snowfall": self._number(current["snowfall"], minimum=0) * 3600 / interval,
            "visibility": visibility,
            "temperature": temperature,
        }

    def _parse_sun(self, data, day, position):
        daily = data["daily"]
        # With timeformat=unixtime the daily date is a local-midnight epoch.
        index = next(i for i, t in enumerate(daily["time"]) if self._day(t) == day)
        sunrise = self._number(daily["sunrise"][index], minimum=0)
        sunset = self._number(daily["sunset"][index], minimum=0)
        if not sunrise < sunset or self._day(sunrise) != day or self._day(sunset) != day:
            raise ValueError("invalid solar day")
        return {
            "day": day,
            "sunrise": sunrise,
            "sunset": sunset,
            "latitude": position[0],
            "longitude": position[1],
        }

    def poll(self, *, epoch=None, mono=None):
        """Worker-only operation. Tests inject clocks and a local deterministic fetch."""
        epoch = self.wall_clock() if epoch is None else epoch
        mono = self.monotonic() if mono is None else mono
        if not self.policy.enabled:
            return
        position = self._location(epoch)
        if position is None:
            self._set_status(status="waiting_location")
            return
        day = self._day(epoch)
        weather_due = mono >= self._next_weather
        sun_day_due = not isinstance(self._sun, dict) or self._sun.get("day") != day
        # A road trip may leave the cached region. Refresh solar data together
        # with the next hourly weather request, not once per position update.
        sun_due = sun_day_due or (weather_due and not self._near(self._sun, position))
        if (weather_due or sun_due) and mono >= self._retry_after:
            params = {
                "latitude": position[0],
                "longitude": position[1],
                "timezone": self.policy.timezone,
                "timeformat": "unixtime",
                "forecast_days": 1,
            }
            if weather_due:
                params["current"] = "temperature_2m,weather_code,precipitation,snowfall,visibility"
            if sun_due:
                params["daily"] = "sunrise,sunset"
            try:
                data = self.fetch(params, self.policy.timeout_s)
                errors = []
                if weather_due:
                    try:
                        self._weather = self._parse_weather(data, epoch, position)
                        self._next_weather = mono + 3600
                    except (ValueError, TypeError, KeyError):
                        errors.append("invalid_weather")
                if sun_due:
                    try:
                        self._sun = self._parse_sun(data, day, position)
                    except (ValueError, TypeError, KeyError, IndexError, StopIteration):
                        errors.append("invalid_solar")
                self._save_cache()
                if errors:
                    raise ValueError(",".join(errors))
                self._failures = 0
                self._retry_after = 0
                self._set_status(error=None)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._failures += 1
                self._retry_after = mono + min(
                    600, self.policy.retry_s * 2 ** min(self._failures - 1, 4)
                )
                self._set_status(error=str(exc)[:240])
        try:
            weather = self._weather
            valid = bool(
                weather
                and self._near(weather, position)
                and weather["source"] <= epoch < weather["until"]
                and epoch >= weather["received"]
            )
            sun = self._sun
            sun_valid = bool(
                sun
                and sun["day"] == day
                and self._near(sun, position)
                and self._day(sun["sunrise"]) == day
                and self._day(sun["sunset"]) == day
                and sun["sunrise"] < sun["sunset"]
            )
            self._sequence += 1
            model = EnvironmentState(
                Header.now("pi5.environment", self._sequence, capture_time_ns=int(epoch * 1e9)),
                weather=weather_kind(weather["code"]) if valid else Weather.UNKNOWN,
                is_night=not (sun["sunrise"] <= epoch < sun["sunset"]) if sun_valid else False,
                precipitation_mm_h=weather["precipitation"] if valid else 0,
                snowfall_cm_h=weather["snowfall"] if valid else 0,
                visibility_m=weather["visibility"] if valid else None,
                temperature_c=weather["temperature"] if valid else None,
                api_valid=valid,
                weather_time_ns=int(weather["source"] * 1e9) if valid else None,
                weather_valid_until_ns=int(weather["until"] * 1e9) if valid else None,
                daylight_valid=sun_valid,
                sunrise_time_ns=int(sun["sunrise"] * 1e9) if sun_valid else None,
                sunset_time_ns=int(sun["sunset"] * 1e9) if sun_valid else None,
                latitude_deg=position[0],
                longitude_deg=position[1],
            )
            try:
                self._latest.get_nowait()
            except queue.Empty:
                pass
            self._latest.put_nowait(model)
            self._set_status(
                status="ready"
                if valid and sun_valid
                else "partial"
                if valid or sun_valid
                else "unavailable",
                weather_valid=valid,
                daylight_valid=sun_valid,
                next_weather_in_s=max(0, self._next_weather - mono),
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._weather = self._sun = None
            self._set_status(status="invalid_cache", error=str(exc))

    def take_latest(self):
        try:
            return self._latest.get_nowait()
        except queue.Empty:
            return None

    def _run(self):
        while not self._stop.is_set():
            started = self.monotonic()
            self.poll()
            self._stop.wait(max(0.01, 1 - (self.monotonic() - started)))

    def start(self):
        if self.policy.enabled and self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="codrive-environment", daemon=True
            )
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.policy.timeout_s + 1)
