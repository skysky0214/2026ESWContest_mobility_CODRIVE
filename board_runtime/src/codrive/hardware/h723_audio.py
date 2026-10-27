"""H723/INMP441 MIC2 USB serial input -> filtered 16 kHz mono model chunks.

This is separate from the existing CAUD UART protocol. Reading runs independently
of inference so a model invocation cannot leave the 3-Mbaud serial port undrained.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math
import struct
import threading
import time
import zlib

import numpy as np

from .audio_input import PcmChunk


MIC2_HEADER = struct.Struct("<4sBBBBIHHIQI")
INPUT_RATE = 48_000
FRAMES_PER_PACKET = 256
PAYLOAD_BYTES = FRAMES_PER_PACKET * 4
PACKET_BYTES = MIC2_HEADER.size + PAYLOAD_BYTES + 4


@dataclass(frozen=True, slots=True)
class Mic2Frame:
    sequence: int
    sample_index: int
    firmware_dropped: int
    gain: int | None
    pcm16: bytes


class Mic2Decoder:
    """Decode the firmware's v1/v2 header, little-endian PCM and trailing CRC32."""

    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = self.invalid_headers = self.bytes_discarded = 0

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while True:
            offset = self.buffer.find(b"MIC2")
            if offset < 0:
                discarded = max(0, len(self.buffer) - 3)
                self.bytes_discarded += discarded
                del self.buffer[:discarded]
                break
            if offset:
                self.bytes_discarded += offset
                del self.buffer[:offset]
            if len(self.buffer) < MIC2_HEADER.size:
                break
            (_, version, fmt, channels, gain, rate, count, length, sequence, index, dropped) = (
                MIC2_HEADER.unpack_from(self.buffer)
            )
            if (
                version not in (1, 2)
                or (version == 1 and gain != 0)
                or (fmt, channels, rate, count, length)
                != (1, 2, INPUT_RATE, FRAMES_PER_PACKET, PAYLOAD_BYTES)
            ):
                self.invalid_headers += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < PACKET_BYTES:
                break
            expected = struct.unpack_from("<I", self.buffer, PACKET_BYTES - 4)[0]
            if zlib.crc32(self.buffer[: PACKET_BYTES - 4]) != expected:
                self.crc_errors += 1
                del self.buffer[0]
                continue
            frames.append(
                Mic2Frame(
                    sequence,
                    index,
                    dropped,
                    (gain or 256) if version == 2 else None,
                    bytes(self.buffer[MIC2_HEADER.size : PACKET_BYTES - 4]),
                )
            )
            del self.buffer[:PACKET_BYTES]
        return frames


class Decimator48To16:
    """Causal 127-tap low-pass FIR with history and a continuous 3:1 phase.

    Retains the 126 input samples needed by the next packet. Startup output is
    suppressed until the filter has a full real history; no per-packet padding.
    """

    delay = 63
    history_size = 126

    def __init__(self):
        position = np.arange(127, dtype=np.float64) - self.delay
        cutoff = 7200 / INPUT_RATE
        taps = 2 * cutoff * np.sinc(2 * cutoff * position) * np.hamming(127)
        self.taps = taps / taps.sum()
        self.reset()

    def reset(self):
        self.history = np.zeros(self.history_size, dtype=np.float64)
        self.input_count = 0
        self.last_output_index = None

    def feed(self, samples):
        samples = np.asarray(samples, dtype=np.float64)
        if samples.ndim != 1:
            raise ValueError("Resampler requires mono samples")
        if not len(samples):
            return np.empty(0, dtype="<i2")
        combined = np.concatenate((self.history, samples))
        filtered = np.convolve(combined, self.taps, mode="valid")
        indices = self.input_count + np.arange(len(samples))
        selected = (indices >= self.history_size) & (indices % 3 == 0)
        result = filtered[selected]
        if len(result):
            self.last_output_index = int(indices[selected][-1])
        self.input_count += len(samples)
        self.history = combined[-self.history_size :].copy()
        return np.clip(np.rint(result), -32768, 32767).astype("<i2")


def resolve_h723_port(port):
    if port and port != "auto":
        return port
    from serial.tools import list_ports

    matches = [
        p.device
        for p in list_ports.comports()
        if p.vid == 0x0483 and (p.pid == 0x374E or "STLINK" in (p.description or "").upper())
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Found {len(matches)} ST-LINK serial ports; connect CN1 or specify "
            "--device /dev/serial/by-id/<STLINK port> explicitly"
        )
    return matches[0]


class H723Audio:
    def __init__(
        self,
        port="auto",
        baudrate=3_000_000,
        timeout_s=3.0,
        *,
        channel="left",
        startup_drain_s=0.5,
        queue_chunks=64,
    ):
        if channel not in ("left", "right", "mean"):
            raise ValueError("H723 channel must be left, right or mean")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("H723 input timeout must be positive and finite")
        if not math.isfinite(startup_drain_s) or not 0 <= startup_drain_s <= 5:
            raise ValueError("H723 startup drain must be 0–5 seconds")
        if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
            raise ValueError("H723 baudrate must be a positive integer")
        if isinstance(queue_chunks, bool) or not isinstance(queue_chunks, int) or queue_chunks < 1:
            raise ValueError("H723 queue size must be positive")
        self.port, self.baudrate, self.timeout_s = port, baudrate, timeout_s
        self.channel, self.startup_drain_s = channel, startup_drain_s
        self.queue_chunks = queue_chunks
        self.decoder = Mic2Decoder()
        self.resampler = Decimator48To16()
        self.sequence_gaps = self.duplicates = self.restarts = self.gain_changes = 0
        self.missing_input_frames = self.clipped_samples = self.host_queue_drops = 0
        self.firmware_dropped_packets = 0
        self.clock_slew_ns = self.clock_resets = 0
        self.gain = None
        self.last_frame = None
        self.offset_ns = self.segment_start = None
        self._queue = deque()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._error = None
        self._reader = None

    def accept(self, frame, received_ns):
        discontinuity = False
        if self.last_frame is not None:
            previous = self.last_frame
            if frame.sequence == previous.sequence and frame.sample_index == previous.sample_index:
                self.duplicates += 1
                return None
            expected = previous.sample_index + FRAMES_PER_PACKET
            delta = (frame.sequence - previous.sequence) & 0xFFFFFFFF
            if frame.sample_index < expected:
                self.restarts += 1
                discontinuity = True
                self.offset_ns = None
            elif frame.sample_index != expected or delta != 1:
                self.sequence_gaps += 1
                self.missing_input_frames += frame.sample_index - expected
                discontinuity = True
            if previous.gain != frame.gain:
                self.gain_changes += 1
                discontinuity = True
        # MCU HSI and host clocks run at slightly different rates. Slew their
        # mapping without declaring real PCM loss or repeatedly resetting the
        # 960-ms model window. Ignore 1-ms USB jitter, bound each adjustment to
        # 250 us (well below RollingAudio's 2-ms discontinuity threshold).
        observed_offset = (
            received_ns - (frame.sample_index + FRAMES_PER_PACKET) * 10**9 // INPUT_RATE
        )
        if self.offset_ns is None:
            self.offset_ns = observed_offset
        else:
            error = observed_offset - self.offset_ns
            if abs(error) > 100_000_000:
                self.offset_ns = observed_offset
                self.clock_resets += 1
                discontinuity = True
            elif abs(error) > 1_000_000:
                correction = max(
                    -250_000, min(250_000, error - (1_000_000 if error > 0 else -1_000_000))
                )
                self.offset_ns += correction
                self.clock_slew_ns += correction
        if self.segment_start is None or discontinuity:
            self.resampler.reset()
            self.segment_start = frame.sample_index
        self.last_frame, self.gain = frame, frame.gain
        self.firmware_dropped_packets = frame.firmware_dropped
        stereo = np.frombuffer(frame.pcm16, dtype="<i2").reshape(-1, 2)
        mono = (
            stereo[:, 0 if self.channel == "left" else 1].astype(np.float64)
            if self.channel != "mean"
            else stereo.astype(np.float64).mean(axis=1)
        )
        selected = (
            stereo if self.channel == "mean" else stereo[:, 0 if self.channel == "left" else 1]
        )
        self.clipped_samples += int(np.count_nonzero((selected <= -32768) | (selected >= 32767)))
        pcm = self.resampler.feed(mono)
        if not len(pcm):
            return None
        # Account for the FIR group delay and output sample phase in timestamps.
        end_index = self.segment_start + self.resampler.last_output_index + 3 - self.resampler.delay
        end_ns = self.offset_ns + end_index * 10**9 // INPUT_RATE
        return PcmChunk(pcm.tobytes(), end_ns, discontinuity)

    def accept_batch(self, frames, received_ns):
        """Anchor a USB batch at its last frame, not the first buffered frame."""
        if not frames:
            return []
        chunks = []
        last_index = frames[-1].sample_index
        for frame in frames:
            # USB reads can contain many frames. Treating their first frame as
            # received 'now' would incorrectly put later frames into the future.
            age = max(0, last_index - frame.sample_index) * 10**9 // INPUT_RATE
            chunk = self.accept(frame, received_ns - age)
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def stats(self):
        return {
            "protocol": "MIC2",
            "input_rate_hz": INPUT_RATE,
            "output_rate_hz": 16000,
            "channel": self.channel,
            "gain": self.gain,
            "clock_slew_ns": self.clock_slew_ns,
            "clock_resets": self.clock_resets,
            "crc_errors": self.decoder.crc_errors,
            "invalid_headers": self.decoder.invalid_headers,
            "bytes_discarded": self.decoder.bytes_discarded,
            "sequence_gaps": self.sequence_gaps,
            "duplicates": self.duplicates,
            "restarts": self.restarts,
            "gain_changes": self.gain_changes,
            "missing_input_frames": self.missing_input_frames,
            "firmware_dropped_packets": self.firmware_dropped_packets,
            "host_queue_drops": self.host_queue_drops,
            "clipped_samples": self.clipped_samples,
        }

    def _enqueue(self, chunk):
        with self._condition:
            if len(self._queue) >= self.queue_chunks:
                # Drop the old backlog together; the next delivered chunk marks the gap.
                self.host_queue_drops += len(self._queue)
                self._queue.clear()
                chunk = replace(chunk, discontinuity=True)
            self._queue.append(chunk)
            self._condition.notify_all()

    def _read(self):
        try:
            import serial

            with serial.Serial(
                resolve_h723_port(self.port),
                self.baudrate,
                timeout=0.05,
                exclusive=True,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            ) as port:
                port.reset_input_buffer()
                until = time.monotonic() + self.startup_drain_s
                while time.monotonic() < until and not self._stop.is_set():
                    port.read(max(1, min(port.in_waiting, 65536)))
                deadline = time.monotonic() + self.timeout_s
                while not self._stop.is_set():
                    data = port.read(max(1, min(port.in_waiting, 65536)))
                    frames = self.decoder.feed(data)
                    if frames:
                        deadline = time.monotonic() + self.timeout_s
                    for chunk in self.accept_batch(frames, time.time_ns()):
                        self._enqueue(chunk)
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "No valid H723 MIC2 audio; check CN1, port and 3000000 baud"
                        )
        except Exception as exc:
            with self._condition:
                self._error = exc
                self._condition.notify_all()

    def chunks(self):
        if self._reader is not None:
            raise RuntimeError("Create a new H723Audio receiver for each capture session")
        self._reader = threading.Thread(target=self._read, name="h723-audio-reader", daemon=True)
        self._reader.start()
        try:
            while True:
                with self._condition:
                    ready = self._condition.wait_for(
                        lambda: self._queue or self._error is not None,
                        timeout=self.timeout_s + self.startup_drain_s + 1,
                    )
                    if self._error is not None:
                        raise self._error
                    if not ready:
                        raise TimeoutError("H723 audio reader stalled")
                    chunk = self._queue.popleft()
                yield chunk
        finally:
            self._stop.set()
            self._reader.join(timeout=2)
