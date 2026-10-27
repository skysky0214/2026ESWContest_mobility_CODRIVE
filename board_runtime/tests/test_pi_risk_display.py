"""Exercise the actual Pi app with local transport; never create a DDS participant."""

from __future__ import annotations

import json
import threading
import time
from urllib.request import urlopen

import pytest

from codrive.apps import pi
from codrive.apps.local_bus import LocalBus
from codrive.models import AudioEvent, AudioKind, DriverState, Header
from codrive.runtime import publish_model


@pytest.mark.parametrize("model", ["off", "missing", "final-sensors-v3"])
@pytest.mark.parametrize("audio_enabled", [False, True])
def test_model_display_respects_explicit_audio_policy(monkeypatch, model, audio_enabled):
    if model in {"B", "final-sensors-v3"}:
        pytest.importorskip("xgboost")
    now = time.time_ns()
    monkeypatch.setattr(pi.time, "time_ns", lambda: now)
    bus = LocalBus()

    def receive_live_inputs(**kwargs):
        # Observations arrive after the optional model finishes loading at startup.
        publish_model(
            bus,
            "driver_state",
            DriverState(
                Header.now("jetson-cabin.dms", 1),
                "cabin",
                "dms.pt",
                0.2,
                False,
                phone_detected=True,
            ),
        )
        publish_model(
            bus,
            "audio_event",
            AudioEvent(
                Header.now("jetson-cabin.audio", 1),
                AudioKind.SIREN,
                0.99,
                960,
                "audio-event-v1",
            ),
        )
        return bus

    captured = []
    monkeypatch.setattr(bus, "close", lambda: None)
    monkeypatch.setattr(pi, "CycloneDdsBus", receive_live_inputs)
    monkeypatch.setattr(pi.signal, "signal", lambda *args: None)
    monkeypatch.setenv("CYCLONEDDS_URI", "")
    real_update = pi.DashboardState.update

    def record(self, value):
        captured.append(value)
        real_update(self, value)

    monkeypatch.setattr(pi.DashboardState, "update", record)
    args = [
        "--dds-config",
        "config/integration/cyclonedds-pi-wired.xml",
        "--port",
        "0",
        "--duration",
        "0",
        "--cabin-camera-stream",
        "http://127.0.0.1:8082/stream.mjpg",
    ]
    args += ["--audio-alerts" if audio_enabled else "--no-audio-alerts"]
    if model != "off":
        args += ["--xgboost-model", "/missing-model" if model == "missing"
                 else f"artifacts/models/risk/{model}"]
    assert pi.main(args) == 0
    state = captured[-1]
    assert state["audio_input"]["alerts_enabled"] is audio_enabled
    assert not state["speech"]["enabled"]
    assert state["camera_preview"]["cabin_stream_url"] == "http://127.0.0.1:8082/stream.mjpg"
    assert state["command"]["risk_code"] == ("emergency_vehicle" if audio_enabled else "driver_phone_observation")
    assert state["command"]["source"] == "template"
    commands = bus.take_json("hmi_command", 16)
    assert len(commands) == 1
    assert json.loads(commands[0].payload_json)["source"] == "template"
    if model == "B":
        assert state["risk_model"]["mode"] == "display_only"
        assert state["risk_model"]["inputs"]["siren_detected"] is None
    elif model == "final-sensors-v3":
        risk_model = state["risk_model"]
        assert risk_model["mode"] == "display_only"
        assert risk_model["feature_contract_version"] == 3
        assert len(risk_model["inputs"]) == 36
        assert risk_model["inputs"]["audio_kind"] == "siren"
        assert risk_model["inputs"]["audio_direction"] is None
        assert risk_model["inputs"]["driver_behavior"] == "phone"
        assert risk_model["inputs"]["speed_mps"] is None
    else:
        assert state["risk_model"]["status"] == ("disabled" if model == "off" else "model_error")


def test_hmi_serves_script_and_model_json():
    state = pi.DashboardState()
    state.update({"updated_time_ns": time.time_ns(), "risk_model": {"status": "unavailable"}})
    server = pi.make_http_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/state") as response:
            assert json.load(response)["risk_model"]["status"] == "unavailable"
        with urlopen(base + "/dashboard.js") as response:
            assert "javascript" in response.headers["Content-Type"]
            assert "function renderRisk" in response.read().decode()
        with urlopen(base) as response:
            html = response.read().decode()
            assert 'id="hmiRoot"' in html and 'src="/main_hmi_live.js"' in html
            assert 'function activateScenario' not in html
            assert 'id="cabinCamera"' in html and 'id="swapCameras"' in html
        with urlopen(base + "/diagnostics") as response:
            html = response.read().decode()
            assert 'id="modelPanel"' in html and 'src="/dashboard.js"' in html
        with urlopen(base + "/api/hmi") as response:
            value = json.load(response)
            assert value["risk_model"]["status"] == "unavailable"
            assert value["server_time_ns"] > 0
            assert "receive_age_ms" not in value
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
