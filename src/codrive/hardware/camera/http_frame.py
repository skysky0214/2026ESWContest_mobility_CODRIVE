"""Read unique, fresh simulation JPEG snapshots; never replay a cached frame."""
import math
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import urlopen

import cv2
import numpy as np


class HttpFrameCapture:
    def __init__(self, url, *, max_age_ms=250, read_timeout_s=3):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path != '/frame.jpg':
            raise ValueError('Simulation stream must be an HTTP(S) /frame.jpg URL')
        self.url, self.max_age_ms = url, max_age_ms
        self.read_timeout_s = read_timeout_s
        self.last_sequence = None
        self.last_pts_ns = None
        self.capture_time_ns = None
        self.transport_age_upper_ms = None
        self.request_roundtrip_ms = None
        self.relay_frame_age_ms = None
        self.closed = False

    def read(self):
        deadline = time.monotonic()+self.read_timeout_s
        while not self.closed and time.monotonic() < deadline:
            started = time.monotonic()
            try:
                with urlopen(self.url, timeout=2) as response:
                    age = float(response.headers['X-Frame-Age-Ms'])
                    sequence = int(response.headers['X-Frame-Sequence'])
                    data = response.read(4_000_001)
            except HTTPError as exc:
                if exc.code != 503:
                    raise
                time.sleep(.01)
                continue
            elapsed = (time.monotonic()-started)*1000
            bound = age+elapsed
            if not math.isfinite(bound) or age < 0 or bound > self.max_age_ms:
                time.sleep(.01)
                continue
            if len(data) > 4_000_000:
                raise ValueError('Simulation JPEG too large')
            if sequence == self.last_sequence:
                time.sleep(.01)
                continue
            received_ns = time.time_ns()
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Invalid simulation JPEG')
            self.last_sequence = sequence
            # Conservative local timestamp: no assumption that laptop and Jetson clocks match.
            self.capture_time_ns = received_ns-round(bound*1e6)
            self.transport_age_upper_ms = bound
            self.request_roundtrip_ms = elapsed
            self.relay_frame_age_ms = age
            return True, image
        if self.closed:
            return False, None
        raise TimeoutError('No unique fresh simulation frame within the read deadline')

    def release(self):
        self.closed = True
