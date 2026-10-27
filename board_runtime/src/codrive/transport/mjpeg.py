"""Small latest-frame MJPEG server for the non-safety HMI video path.

The perception result still travels over DDS.  This server is deliberately a
separate best-effort path: slow or disconnected HMI clients cannot queue camera
frames or block the risk publisher.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Lock, Thread
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MjpegStreamConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    path: str = "/stream.mjpg"
    width: int = 960
    fps: float = 12.0
    jpeg_quality: int = 70

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise ValueError("port must be in [0, 65535]")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError("path must be an absolute URL path without query or fragment")
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width < 160:
            raise ValueError("width must be an integer of at least 160 pixels")
        if isinstance(self.fps, bool) or not isinstance(self.fps, (int, float)) or self.fps <= 0:
            raise ValueError("fps must be positive")
        if (
            isinstance(self.jpeg_quality, bool)
            or not isinstance(self.jpeg_quality, int)
            or not 20 <= self.jpeg_quality <= 95
        ):
            raise ValueError("jpeg_quality must be in [20, 95]")


class MjpegStreamServer:
    """Serve one latest-frame MJPEG stream and asynchronously encode BGR frames."""

    def __init__(self, config: MjpegStreamConfig | None = None) -> None:
        self.config = config or MjpegStreamConfig()
        self._jpeg_condition = Condition()
        self._latest_jpeg: bytes | None = None
        self._jpeg_sequence = 0
        self._last_jpeg_ns: int | None = None
        self._frame_condition = Condition()
        self._pending_frame: Any | None = None
        self._next_submit_at = 0.0
        self._state_lock = Lock()
        self._closed = False
        self._started = False
        self._httpd: ThreadingHTTPServer | None = None
        self._server_thread: Thread | None = None
        self._encoder_thread: Thread | None = None

    @property
    def port(self) -> int:
        return self.config.port if self._httpd is None else int(self._httpd.server_port)

    @property
    def stream_url(self) -> str:
        host = "127.0.0.1" if self.config.host in {"0.0.0.0", "::"} else self.config.host
        return f"http://{host}:{self.port}{self.config.path}"

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("MJPEG server is closed")
            if self._started:
                return
            handler = self._make_handler()
            self._httpd = ThreadingHTTPServer((self.config.host, self.config.port), handler)
            self._httpd.daemon_threads = True
            self._server_thread = Thread(
                target=self._httpd.serve_forever,
                name="codrive-mjpeg-http",
                daemon=True,
            )
            self._encoder_thread = Thread(
                target=self._encode_loop,
                name="codrive-mjpeg-encoder",
                daemon=True,
            )
            self._started = True
            self._server_thread.start()
            self._encoder_thread.start()

    def submit_bgr(self, frame: Any) -> bool:
        """Queue a copied BGR frame, dropping it when the configured FPS is exceeded."""

        now = time.monotonic()
        with self._frame_condition:
            if self._closed:
                return False
            if now < self._next_submit_at:
                return False
            copy = getattr(frame, "copy", None)
            if not callable(copy):
                raise TypeError("frame must provide copy()")
            self._next_submit_at = now + 1.0 / float(self.config.fps)
            # A single pending slot guarantees that a slow encoder never creates latency backlog.
            self._pending_frame = copy()
            self._frame_condition.notify_all()
        return True

    def publish_jpeg(self, jpeg: bytes | bytearray | memoryview) -> None:
        """Publish a pre-encoded frame. Primarily useful for tests and alternate encoders."""

        payload = bytes(jpeg)
        if not payload:
            raise ValueError("jpeg must not be empty")
        with self._jpeg_condition:
            if self._closed:
                return
            self._latest_jpeg = payload
            self._jpeg_sequence += 1
            self._last_jpeg_ns = time.monotonic_ns()
            self._jpeg_condition.notify_all()

    def _encode_loop(self) -> None:
        while True:
            with self._frame_condition:
                self._frame_condition.wait_for(
                    lambda: self._closed or self._pending_frame is not None
                )
                if self._closed:
                    return
                frame = self._pending_frame
                self._pending_frame = None
            try:
                import cv2

                height, width = frame.shape[:2]
                if width > self.config.width:
                    output_height = max(1, round(height * self.config.width / width))
                    frame = cv2.resize(
                        frame,
                        (self.config.width, output_height),
                        interpolation=cv2.INTER_AREA,
                    )
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality],
                )
                if ok:
                    self.publish_jpeg(encoded.tobytes())
            except Exception:
                # Preview failure must never terminate or delay the perception process.
                logging.getLogger(__name__).exception("HMI video encode failed")

    def _wait_for_jpeg(self, after_sequence: int) -> tuple[int, bytes | None, bool]:
        with self._jpeg_condition:
            self._jpeg_condition.wait_for(
                lambda: self._closed or self._jpeg_sequence > after_sequence,
                timeout=2.0,
            )
            return self._jpeg_sequence, self._latest_jpeg, self._closed

    def _health_payload(self) -> bytes:
        with self._jpeg_condition:
            age_ms = None
            if self._last_jpeg_ns is not None:
                age_ms = (time.monotonic_ns() - self._last_jpeg_ns) / 1_000_000
            payload = {
                "status": "stopped" if self._closed else "ok",
                "sequence": self._jpeg_sequence,
                "frame_age_ms": age_ms,
                "stream": self.config.path,
            }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/health":
                    payload = owner._health_payload()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path != owner.config.path:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame"
                )
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                sequence = 0
                try:
                    while True:
                        sequence, jpeg, closed = owner._wait_for_jpeg(sequence)
                        if closed:
                            return
                        if jpeg is None:
                            continue
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        with self._frame_condition:
            self._frame_condition.notify_all()
        with self._jpeg_condition:
            self._jpeg_condition.notify_all()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        for thread in (self._server_thread, self._encoder_thread):
            if thread is not None:
                thread.join(timeout=3.0)

    def __enter__(self) -> MjpegStreamServer:  # noqa: PYI034 - supports Python 3.10
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> bool:
        self.close()
        return False


__all__ = ["MjpegStreamConfig", "MjpegStreamServer"]
