"""Receive-only PCM sources: strict WAV, ALSA arecord and framed STM32 UART."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass

from .stm32_audio import AudioStreamDecoder


@dataclass(frozen=True, slots=True)
class PcmChunk:
    pcm16: bytes
    end_ns: int
    discontinuity: bool = False


def wav_chunks(path, chunk_samples=320):
    with wave.open(str(path), "rb") as reader:
        if (
            reader.getframerate(),
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getcomptype(),
        ) != (16000, 1, 2, "NONE"):
            raise ValueError("WAV must be uncompressed 16kHz mono PCM16")
        origin, samples = time.time_ns(), 0
        while payload := reader.readframes(chunk_samples):
            samples += len(payload) // 2
            yield PcmChunk(payload, origin + samples * 62500)


def alsa_chunks(device="plughw:CARD=Webcam,DEV=0", timeout_s=3.0):
    if shutil.which("arecord") is None:
        raise RuntimeError("arecord is missing; install alsa-utils")
    command = ["arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]
    origin = time.time_ns()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    pending, samples = bytearray(), 0
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            if not selector.select(timeout_s):
                raise TimeoutError("ALSA microphone produced no PCM within timeout")
            data = os.read(process.stdout.fileno(), 640)
            if not data:
                raise RuntimeError(f"arecord stopped; check ALSA device {device} and permissions")
            pending.extend(data)
            while len(pending) >= 640:
                payload = bytes(pending[:640])
                del pending[:640]
                samples += 320
                end_ns = origin + samples * 62500
                received_ns = time.time_ns()
                discontinuity = end_ns > received_ns + 2_000_000
                if end_ns > received_ns:
                    origin -= end_ns - received_ns
                    end_ns = received_ns
                yield PcmChunk(payload, end_ns, discontinuity)
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdout.close()


class SerialAudio:
    def __init__(self, port, baudrate=921600, timeout_s=3.0):
        if not port:
            raise ValueError("Specify the actual STM32 UART device; do not guess ttyTHS0")
        self.port, self.baudrate, self.timeout_s = port, baudrate, timeout_s
        self.decoder = AudioStreamDecoder()
        self.sequence_gaps = self.duplicates = self.restarts = 0
        self.last_sequence = self.last_timestamp = self.offset_ns = None

    def accept(self, frame, received_ns):
        discontinuity = False
        if self.last_sequence is not None:
            delta = (frame.sequence - self.last_sequence) & 0xFFFFFFFF
            if delta == 0 and frame.timestamp_us == self.last_timestamp:
                self.duplicates += 1
                return None
            if frame.timestamp_us <= self.last_timestamp:
                self.restarts += 1
                self.offset_ns = None
                discontinuity = True
            elif delta != 1:
                self.sequence_gaps += 1
                discontinuity = True
        # STM32 timestamp_us is first PCM sample since boot; map to host epoch.
        end_device = frame.timestamp_us * 1000 + frame.sample_count * 62500
        if self.offset_ns is None:
            self.offset_ns = received_ns - end_device
        mapped_end = end_device + self.offset_ns
        if mapped_end > received_ns + 2_000_000:
            self.offset_ns -= mapped_end - received_ns
            mapped_end = received_ns
            discontinuity = True
        self.last_sequence, self.last_timestamp = frame.sequence, frame.timestamp_us
        return PcmChunk(frame.pcm16, mapped_end, discontinuity)

    def stats(self):
        return {
            "crc_errors": self.decoder.crc_errors,
            "invalid_headers": self.decoder.invalid_headers,
            "bytes_discarded": self.decoder.bytes_discarded,
            "sequence_gaps": self.sequence_gaps,
            "duplicates": self.duplicates,
            "restarts": self.restarts,
        }

    def chunks(self):
        import serial

        with serial.Serial(self.port, self.baudrate, timeout=0.1) as port:
            deadline = time.monotonic() + self.timeout_s
            while True:
                data = port.read(min(max(port.in_waiting, 1), 4096))
                for frame in self.decoder.feed(data):
                    chunk = self.accept(frame, time.time_ns())
                    if chunk is not None:
                        deadline = time.monotonic() + self.timeout_s
                        yield chunk
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "No valid STM32 audio frame within timeout (check port, wiring, CRC, firmware)"
                    )
