from __future__ import annotations

import json
import time
from urllib.request import urlopen

from codrive.contracts.models import Header, SituationSnapshot, VehicleState
from codrive.hmi.web.server import HmiTelemetryStore, HmiWebConfig, HmiWebServer


NOW_NS = 4_000_000_000_000


def vehicle_snapshot(*, vehicle: VehicleState | None) -> SituationSnapshot:
    return SituationSnapshot(
        header=Header.now("pi5.fusion", 1, capture_time_ns=NOW_NS),
        vehicle=vehicle,
        stale_sources=() if vehicle is not None else ("vehicle",),
    )


def vehicle_state(*, can_valid: bool = True) -> VehicleState:
    return VehicleState(
        header=Header.now("pi5.panda", 17, capture_time_ns=NOW_NS),
        speed_mps=20.0,
        steering_wheel_deg=-14.5,
        brake_pressed=True,
        accelerator_pct=27.25,
        can_valid=can_valid,
    )


def test_store_maps_vehicle_state_to_hmi_units_and_fields() -> None:
    monotonic_ns = 1_000_000_000
    store = HmiTelemetryStore(monotonic_clock_ns=lambda: monotonic_ns)

    store.update_snapshot(vehicle_snapshot(vehicle=vehicle_state()), observed_time_ns=NOW_NS)
    payload = store.payload()

    assert payload["connected"] is True
    assert payload["stale"] is False
    assert payload["vehicle"] == {
        "speed_kmh": 72.0,
        "steering_wheel_deg": -14.5,
        "brake_pressed": True,
        "accelerator_pct": 27.25,
        "can_valid": True,
        "source_id": "pi5.panda",
        "sequence": 17,
        "capture_time_ns": NOW_NS,
    }


def test_store_marks_missing_or_expired_can_data_disconnected() -> None:
    clock = [1_000_000_000]
    store = HmiTelemetryStore(
        stale_after_ms=500,
        monotonic_clock_ns=lambda: clock[0],
    )
    store.update_snapshot(vehicle_snapshot(vehicle=vehicle_state()), observed_time_ns=NOW_NS)

    clock[0] += 501_000_000
    assert store.payload()["connected"] is False

    clock[0] += 1
    store.update_snapshot(vehicle_snapshot(vehicle=None), observed_time_ns=NOW_NS + 1)
    payload = store.payload()
    assert payload["connected"] is False
    assert payload["vehicle"] is not None


def test_store_rejects_can_invalid_state() -> None:
    store = HmiTelemetryStore()
    store.update_snapshot(
        vehicle_snapshot(vehicle=vehicle_state(can_valid=False)),
        observed_time_ns=NOW_NS,
    )

    assert store.payload()["connected"] is False


def test_web_server_serves_hmi_and_vehicle_api() -> None:
    store = HmiTelemetryStore()
    store.update_snapshot(vehicle_snapshot(vehicle=vehicle_state()), observed_time_ns=NOW_NS)
    server = HmiWebServer(HmiWebConfig(host="127.0.0.1", port=0), store=store)

    server.start()
    try:
        with urlopen(server.url, timeout=2) as response:
            assert response.status == 200
            assert b"CODRIVE HMI" in response.read()
        with urlopen(f"{server.url}api/vehicle", timeout=2) as response:
            payload = json.load(response)
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert payload["connected"] is True
            assert payload["vehicle"]["speed_kmh"] == 72.0
    finally:
        server.close()


def test_server_context_manager_closes_promptly() -> None:
    started = time.monotonic()
    with HmiWebServer(HmiWebConfig(host="127.0.0.1", port=0)) as server:
        assert server.port > 0
    assert time.monotonic() - started < 2.0
