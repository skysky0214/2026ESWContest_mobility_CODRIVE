"""Local HTTP bridge from the Pi situation snapshot to the browser HMI.

The bridge deliberately exposes read-only telemetry.  It has no CAN transmit
surface and uses only Python's standard library so it can run beside the Pi
orchestrator without adding another deployment dependency.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.parse import urlsplit

from ...contracts.models import SituationSnapshot, VehicleState


@dataclass(frozen=True, slots=True)
class HmiWebConfig:
    host: str = "0.0.0.0"
    port: int = 8090
    stale_after_ms: int = 500
    access_log: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError("port must be an integer")
        if not 0 <= self.port <= 65_535:
            raise ValueError("port must be in [0, 65535]")
        if (
            isinstance(self.stale_after_ms, bool)
            or not isinstance(self.stale_after_ms, int)
            or self.stale_after_ms < 100
        ):
            raise ValueError("stale_after_ms must be an integer >= 100")


class HmiTelemetryStore:
    """Thread-safe latest-value store populated by :class:`PiOrchestrator`."""

    def __init__(
        self,
        *,
        stale_after_ms: int = 500,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            isinstance(stale_after_ms, bool)
            or not isinstance(stale_after_ms, int)
            or stale_after_ms < 100
        ):
            raise ValueError("stale_after_ms must be an integer >= 100")
        self.stale_after_ms = stale_after_ms
        self._monotonic_clock_ns = monotonic_clock_ns
        self._lock = RLock()
        self._vehicle: VehicleState | None = None
        self._vehicle_fresh = False
        self._snapshot_time_ns = 0
        self._last_update_monotonic_ns: int | None = None

    def update_snapshot(
        self,
        snapshot: SituationSnapshot,
        *,
        observed_time_ns: int | None = None,
    ) -> None:
        """Store vehicle telemetry and the fusion layer's freshness decision."""

        if not isinstance(snapshot, SituationSnapshot):
            raise TypeError("snapshot must be a SituationSnapshot")
        observed = time.time_ns() if observed_time_ns is None else observed_time_ns
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise ValueError("observed_time_ns must be a non-negative integer")
        with self._lock:
            if snapshot.vehicle is not None:
                self._vehicle = snapshot.vehicle
            self._vehicle_fresh = snapshot.vehicle is not None
            self._snapshot_time_ns = observed
            self._last_update_monotonic_ns = self._monotonic_clock_ns()

    def payload(self, *, monotonic_ns: int | None = None) -> dict[str, Any]:
        """Return the JSON-ready HMI payload without exposing mutable state."""

        current_monotonic_ns = (
            self._monotonic_clock_ns() if monotonic_ns is None else monotonic_ns
        )
        with self._lock:
            vehicle = self._vehicle
            updated_monotonic_ns = self._last_update_monotonic_ns
            fusion_fresh = self._vehicle_fresh
            snapshot_time_ns = self._snapshot_time_ns

        if updated_monotonic_ns is None:
            bridge_age_ms: float | None = None
            bridge_fresh = False
        else:
            bridge_age_ms = max(
                0.0, (current_monotonic_ns - updated_monotonic_ns) / 1_000_000.0
            )
            bridge_fresh = bridge_age_ms <= self.stale_after_ms

        connected = bool(
            vehicle is not None and fusion_fresh and bridge_fresh and vehicle.can_valid
        )
        vehicle_payload = None
        if vehicle is not None:
            vehicle_payload = {
                "speed_kmh": vehicle.speed_mps * 3.6,
                "steering_wheel_deg": vehicle.steering_wheel_deg,
                "brake_pressed": vehicle.brake_pressed,
                "accelerator_pct": vehicle.accelerator_pct,
                "can_valid": vehicle.can_valid,
                "source_id": vehicle.header.source_id,
                "sequence": vehicle.header.sequence,
                "capture_time_ns": vehicle.header.capture_time_ns,
            }

        return {
            "schema_version": 1,
            "connected": connected,
            "stale": not connected,
            "bridge_age_ms": bridge_age_ms,
            "snapshot_time_ns": snapshot_time_ns,
            "vehicle": vehicle_payload,
        }


class HmiWebServer:
    """Serve the HMI page and its same-origin vehicle telemetry endpoint."""

    def __init__(
        self,
        config: HmiWebConfig | None = None,
        *,
        store: HmiTelemetryStore | None = None,
        index_path: str | Path | None = None,
    ) -> None:
        self.config = config or HmiWebConfig()
        self.store = store or HmiTelemetryStore(
            stale_after_ms=self.config.stale_after_ms
        )
        self.index_path = (
            Path(index_path)
            if index_path is not None
            else Path(__file__).with_name("index.html")
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self.config.port
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        host = self.config.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/"

    def update_snapshot(
        self,
        snapshot: SituationSnapshot,
        *,
        observed_time_ns: int | None = None,
    ) -> None:
        self.store.update_snapshot(snapshot, observed_time_ns=observed_time_ns)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CODRIVE-HMI/1"

            def _send(
                self,
                status: HTTPStatus,
                content_type: str,
                body: bytes,
                *,
                cors: bool = False,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                path = urlsplit(self.path).path
                if path in {"/", "/index.html"}:
                    try:
                        body = bridge.index_path.read_bytes()
                    except OSError:
                        self._send(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "text/plain; charset=utf-8",
                            "HMI index unavailable".encode(),
                        )
                        return
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)
                    return
                if path == "/api/vehicle":
                    body = json.dumps(
                        bridge.store.payload(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._send(
                        HTTPStatus.OK,
                        "application/json; charset=utf-8",
                        body,
                        cors=True,
                    )
                    return
                if path == "/health":
                    body = json.dumps(
                        {
                            "ok": True,
                            "vehicle_connected": bridge.store.payload()["connected"],
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._send(
                        HTTPStatus.OK,
                        "application/json; charset=utf-8",
                        body,
                        cors=True,
                    )
                    return
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Not found",
                )

            def log_message(self, message_format: str, *args: object) -> None:
                if bridge.config.access_log:
                    super().log_message(message_format, *args)

        return Handler

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("HMI web server is already running")
        server = ThreadingHTTPServer(
            (self.config.host, self.config.port), self._handler_type()
        )
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, name="codrive-hmi-web", daemon=True)
        self._server = server
        self._thread = thread
        thread.start()

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def __enter__(self) -> HmiWebServer:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = ["HmiTelemetryStore", "HmiWebConfig", "HmiWebServer"]
