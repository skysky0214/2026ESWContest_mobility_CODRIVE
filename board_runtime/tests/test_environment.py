import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from codrive.environment import EnvironmentPolicy, EnvironmentWorker
from codrive.models import Weather

ZONE = ZoneInfo("Asia/Seoul")
NOW = datetime(2020, 10, 26, 12, tzinfo=ZONE).timestamp()


def reply(epoch=NOW):
    day = datetime.fromtimestamp(epoch, ZONE)
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {
        "current": {
            "time": epoch,
            "interval": 900,
            "weather_code": 61,
            "temperature_2m": 12,
            "precipitation": 1,
            "snowfall": 0,
            "visibility": 800,
        },
        "daily": {
            "time": [midnight],
            "sunrise": [midnight + 6 * 3600],
            "sunset": [midnight + 18 * 3600],
        },
    }


def worker(tmp_path, fetch, **kwargs):
    return EnvironmentWorker(
        EnvironmentPolicy(
            latitude=37.5, longitude=127, cache_path=str(tmp_path / "weather.json"), **kwargs
        ),
        fetch=fetch,
        wall_clock=lambda: NOW,
        monotonic=lambda: 0,
    )


def test_hourly_weather_daily_sun_and_cached_restart(tmp_path):
    calls = []

    def fetch(params, timeout):
        calls.append(params)
        return reply(NOW + len(calls) * 0)

    w = worker(tmp_path, fetch)
    w.poll(epoch=NOW, mono=0)
    sample = w.take_latest()
    assert sample.weather is Weather.RAIN and sample.precipitation_mm_h == 4
    assert sample.daylight_valid and not sample.is_night
    for mono in (1, 59, 3599):
        w.poll(epoch=NOW + mono, mono=mono)
    assert len(calls) == 1
    w.poll(epoch=NOW + 3600, mono=3600)
    assert len(calls) == 2 and "daily" not in calls[1]
    # Restart at the same time uses today's solar and weather cache.
    again = EnvironmentWorker(
        w.policy, fetch=fetch, wall_clock=lambda: NOW + 3600, monotonic=lambda: 0
    )
    again.poll(epoch=NOW + 3600, mono=0)
    assert len(calls) == 2


def test_solar_only_request_after_date_change_and_night_boundary(tmp_path):
    calls = []
    epochs = [NOW, NOW + 12 * 3600]

    def fetch(params, timeout):
        calls.append(params)
        return reply(epochs[len(calls) - 1])

    w = worker(tmp_path, fetch)
    w.poll(epoch=NOW, mono=0)
    w.take_latest()
    w.poll(epoch=NOW + 6 * 3600, mono=1)
    assert w.take_latest().is_night
    w.poll(epoch=NOW + 12 * 3600, mono=2)
    assert len(calls) == 2 and "current" not in calls[1] and "daily" in calls[1]
    assert w.take_latest().daylight_valid


def test_weather_error_backoff_does_not_become_clear_and_sun_survives(tmp_path):
    calls = []

    def fetch(params, timeout):
        calls.append(params)
        if len(calls) > 1:
            raise OSError("offline")
        return reply()

    w = worker(tmp_path, fetch)
    w.poll(epoch=NOW, mono=0)
    w.poll(epoch=NOW + 3600, mono=3600)
    w.poll(epoch=NOW + 3601, mono=3601)
    assert len(calls) == 2
    w.poll(epoch=NOW + 4501, mono=4501)
    state = w.take_latest()
    assert not state.api_valid and state.weather is Weather.UNKNOWN
    assert state.daylight_valid


def test_missing_location_does_not_use_guessed_city(tmp_path):
    def forbidden(*args):
        pytest.fail("no position must mean no HTTP")

    w = EnvironmentWorker(EnvironmentPolicy(cache_path=str(tmp_path / "missing")), fetch=forbidden)
    w.poll(epoch=NOW, mono=0)
    assert w.take_latest() is None and w.status()["status"] == "waiting_location"


def test_http_worker_never_blocks_take_latest(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def fetch(*args):
        entered.set()
        release.wait(2)
        return reply()

    w = worker(tmp_path, fetch)
    try:
        w.start()
        assert entered.wait(1)
        start = time.perf_counter()
        assert w.take_latest() is None
        assert time.perf_counter() - start < 0.05
    finally:
        release.set()
        w.close()


@pytest.mark.parametrize("damage", ["future", "nan", "invalid_sun"])
def test_invalid_api_values_do_not_create_valid_inputs(tmp_path, damage):
    data = reply()
    if damage == "future":
        data["current"]["time"] = NOW + 3600
    if damage == "nan":
        data["current"]["visibility"] = float("nan")
    if damage == "invalid_sun":
        data["daily"]["sunset"] = [NOW - 86400]
    w = worker(tmp_path, lambda *args: data)
    w.poll(epoch=NOW, mono=0)
    sample = w.take_latest()
    if damage == "invalid_sun":
        assert not sample.daylight_valid and sample.api_valid
    else:
        assert not sample.api_valid and sample.daylight_valid


def test_corrupt_disk_cache_is_rejected_and_refetched(tmp_path):
    import json

    w = worker(tmp_path, lambda *args: reply())
    w.poll(epoch=NOW, mono=0)
    path = tmp_path / "weather.json"
    data = json.loads(path.read_text())
    data["weather"]["received"] = NOW + 86400
    path.write_text(json.dumps(data))
    calls = []
    again = worker(tmp_path, lambda *args: (calls.append(args), reply())[1])
    again.poll(epoch=NOW, mono=0)
    assert len(calls) == 1 and again.take_latest().api_valid


def test_region_change_waits_for_next_hourly_request(tmp_path):
    calls = []

    def fetch(params, timeout):
        calls.append(params)
        return reply(NOW + (3600 if len(calls) > 1 else 0))

    w = worker(tmp_path, fetch)
    w.poll(epoch=NOW, mono=0)
    with w._lock:
        w._position = (35.2, 129, None)
    w.poll(epoch=NOW + 1, mono=1)
    state = w.take_latest()
    assert len(calls) == 1 and not state.api_valid and not state.daylight_valid
    w.poll(epoch=NOW + 3600, mono=3600)
    assert len(calls) == 2 and "daily" in calls[1]
    assert w.take_latest().daylight_valid
