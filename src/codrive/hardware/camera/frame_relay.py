"""Latest JPEG snapshot relay for a simulation camera, with clock-independent age."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FrameRelay:
    def __init__(self, host='0.0.0.0', port=18082, max_age_ms=250):
        self.lock = threading.Lock()
        self.frame = None
        self.sequence = 0
        self.max_age_ms = max_age_ms
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with owner.lock:
                    frame, sequence = owner.frame, owner.sequence
                age = (time.monotonic()-frame[1])*1000 if frame else None
                fresh = age is not None and 0 <= age <= owner.max_age_ms
                if self.path == '/health':
                    body = json.dumps({'ready': fresh, 'sequence': sequence, 'frame_age_ms': age,
                                       'source': 'carla_rgb_image_receipt', 'ground_truth_injected': False}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                elif self.path == '/frame.jpg' and fresh:
                    body = frame[0]
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('X-Frame-Age-Ms', f'{age:.3f}')
                    self.send_header('X-Frame-Sequence', str(sequence))
                else:
                    body = b'Camera frame unavailable or stale'
                    self.send_response(503 if self.path == '/frame.jpg' else 404)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def publish(self, jpeg, received_monotonic):
        with self.lock:
            self.sequence += 1
            self.frame = bytes(jpeg), received_monotonic

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
