from __future__ import annotations

import json
import threading
import urllib.request

from codrive.apps.dms import run_camera_loop
from codrive.apps.local_bus import LocalBus
from codrive.apps.pi import DashboardState, make_http_server
from codrive.fusion import FusionPolicy, SituationFusion
from codrive.health import HealthPolicy, NodeHealthMonitor
from codrive.models import AudioEvent, AudioKind, DriverState, Header, NodeHealth, Severity, to_json
from codrive.risk import RiskEngine, RiskPolicy
from codrive.runtime import PiOrchestrator, decode_model, publish_model
from codrive.sensor_input.driver.dms import UltralyticsYoloDms
from codrive.hardware.camera import UsbCameraConfig


def driver(now, sequence=1, **kwargs):
    return DriverState(Header.now("jetson-cabin.dms", sequence, capture_time_ns=now),
                       "cabin", "fixture", 0.9, True, **kwargs)


def test_cabin_observation_is_info_without_inventing_vehicle_speed():
    now = 5_000_000_000_000
    bus = LocalBus()
    publish_model(bus, "driver_state", driver(now, phone_detected=True))
    pi = PiOrchestrator(bus, fusion=SituationFusion(policy=FusionPolicy(expected_camera_ids=())),
                        risk_engine=RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)))
    result = pi.step(now_ns=now)
    assert result.snapshot.vehicle is None
    assert result.risk.severity is Severity.INFO
    assert result.hmi.display_text == "휴대폰 사용 감지"
    assert pi.received_samples["driver_state"] == 1
    # Expiry removes the driver and prevents a repeated observation from stale data.
    result = pi.step(now_ns=now + 500_000_000)
    assert result.snapshot.driver is None
    assert result.risk is None


def test_missing_driver_never_generates_phone_or_drowsiness_advisory():
    now = 5_000_000_000_000
    fusion = SituationFusion()
    fusion.ingest(driver(now, face_detected=False, phone_detected=True, drowsiness_score=1),
                  received_time_ns=now)
    snapshot = fusion.snapshot(now_ns=now)
    assert "driver:face_missing" in snapshot.degraded_reasons
    assert RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(snapshot, now_ns=now) is None


def test_bench_audio_stays_visible_without_overriding_driver_advisory():
    now = 5_000_000_000_000
    bus = LocalBus()
    publish_model(bus, "driver_state", driver(now, phone_detected=True))
    publish_model(bus, "audio_event", AudioEvent(
        Header.now("jetson-cabin.audio", 1, capture_time_ns=now),
        AudioKind.SIREN, 0.99, 960, "fixture",
    ))
    pi = PiOrchestrator(bus, risk_engine=RiskEngine(policy=RiskPolicy(
        cabin_observation_enabled=True, audio_alerts_enabled=False,
    )))
    result = pi.step(now_ns=now)
    assert result.snapshot.audio.kind is AudioKind.SIREN
    assert result.risk.code == "driver_phone_observation"
    assert result.hmi.severity is Severity.INFO


def test_source_allowlist_and_restart_sequence_recovery():
    now = 5_000_000_000_000
    fusion = SituationFusion(policy=FusionPolicy(source_allowlist=(("driver:cabin", "jetson-cabin.dms"),)))
    first = driver(now, sequence=50)
    assert fusion.ingest(first, received_time_ns=now)
    restarted = DriverState(Header.now("jetson-cabin.dms", 1, capture_time_ns=now + 1,
                                       boot_id="new-boot"), "cabin", "fixture", 1, True)
    assert fusion.ingest(restarted, received_time_ns=now + 1)
    spoof = DriverState(Header.now("unexpected", 1, capture_time_ns=now + 2),
                        "cabin", "fixture", 1, True)
    assert not fusion.ingest(spoof, received_time_ns=now + 2)


def test_published_dms_health_is_accepted_by_pi_monitor():
    # Exercise the actual camera-loop health envelope, with a bounded fake capture.
    from test_dms import FakeCapture, _FakeYolo, _Result
    bus = LocalBus()
    run_camera_loop(bus=bus, dms=UltralyticsYoloDms("unused", model=_FakeYolo(_Result())),
                    camera=UsbCameraConfig(), capture=FakeCapture(count=1), max_frames=1)
    monitor = NodeHealthMonitor(policy=HealthPolicy(expected_nodes=("jetson-cabin",)))
    for wire in bus.take_json("node_health", 16):
        health = decode_model(wire, NodeHealth)
        assert health.header.source_id == health.node_id
        assert monitor.ingest(health)
    assert monitor.missing_nodes() == ()


def test_old_driver_payload_defaults_and_dashboard_api():
    sample = driver(5_000_000_000_000)
    data = json.loads(to_json(sample))
    for field in ("drowsiness_score", "microsleep_ms", "yawning"):
        del data[field]
    from codrive.models import from_json
    assert from_json(DriverState, json.dumps(data)).drowsiness_score == 0
    state = DashboardState()
    state.update({"status": "live", "driver": data})
    server = make_http_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(address + "/api/state") as response:
            assert json.load(response)["status"] == "live"
        with urllib.request.urlopen(address) as response:
            assert "운전자 상태" in response.read().decode()
        with urllib.request.urlopen(urllib.request.Request(address, method="HEAD")) as response:
            assert response.status == 200
            assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_dummy_sink_does_not_count_as_successful_speech(monkeypatch):
    from types import SimpleNamespace
    from codrive.apps.pi import LocalSpeech
    from codrive.guidance import HmiPolicy

    now = 5_000_000_000_000
    fusion = SituationFusion()
    fusion.ingest(driver(now, phone_detected=True), received_time_ns=now)
    risk = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True)).evaluate(
        fusion.snapshot(now_ns=now), now_ns=now)
    from dataclasses import replace
    import time
    command = replace(HmiPolicy().render(risk), expires_at_ns=time.time_ns() + 10_000_000_000)
    speech = LocalSpeech(False)
    values = iter((command, None))
    speech.pending = SimpleNamespace(get=lambda: next(values))
    calls = []
    def run(args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(stdout="auto_null\n")
    monkeypatch.setattr("codrive.apps.pi.subprocess.run", run)
    speech._run()
    assert calls == ["pactl"]
    assert speech.snapshot()["completed"] == 0
    assert "dummy sink" in speech.snapshot()["error"]


def test_video_replay_requires_local_transport():
    import pytest
    from codrive.apps.dms import main
    with pytest.raises(SystemExit) as error:
        main(["--video", "fixture.mp4"])
    assert error.value.code == 2
