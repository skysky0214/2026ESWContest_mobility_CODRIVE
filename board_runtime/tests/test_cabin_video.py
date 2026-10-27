"""DMS and MJPEG share one capture; preview errors cannot remove driver updates."""

from __future__ import annotations

import socket

import pytest
from test_dms import FakeCapture, _FakeYolo, _Result

from codrive.apps.dms import run_camera_loop
from codrive.apps.local_bus import LocalBus
from codrive.hardware.camera import UsbCameraConfig
from codrive.sensor_input.driver.dms import UltralyticsYoloDms


@pytest.mark.parametrize("broken_preview", [False, True])
def test_preview_uses_captured_frames_and_dms_survives_sink_error(broken_preview):
    bus, capture, seen = LocalBus(), FakeCapture(count=3), []
    dms = UltralyticsYoloDms("unused", model=_FakeYolo(_Result()))

    def sink(frame):
        seen.append(frame)
        if broken_preview:
            raise RuntimeError("preview unavailable")

    assert run_camera_loop(
        bus=bus, dms=dms, camera=UsbCameraConfig(), capture=capture,
        max_frames=3, rate_hz=1000, frame_sink=sink,
    ) == 3
    assert len(bus.take_json("driver_state", 16)) == 3
    assert capture.released
    assert len(seen) == (1 if broken_preview else 3)
    assert seen[0] is dms.model.calls[0]["frame"]


def test_busy_preview_port_does_not_stop_dms(monkeypatch):
    from codrive.apps import dms as app

    capture = FakeCapture(count=1)
    real_dms = UltralyticsYoloDms("unused", model=_FakeYolo(_Result()))
    monkeypatch.setattr(app, "UltralyticsYoloDms", lambda *args, **kwargs: real_dms)
    monkeypatch.setattr(app, "open_opencv_camera", lambda camera: capture)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert app.main([
            "--config", "config/integration/jetson-cabin.yaml", "--dry-run", "--max-frames", "1",
            "--hmi-stream-host", "127.0.0.1", "--hmi-stream-port", str(occupied.getsockname()[1]),
        ]) == 0
    assert len(real_dms.model.calls) == 1
    assert capture.released
