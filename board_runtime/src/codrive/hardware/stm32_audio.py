"""Framing protocol for PCM16 audio streamed from an STM32 over USART.

Wire integers are big-endian.  A version-1 frame is::

    magic[4] | version:u8 | sequence:u32 | timestamp_us:u64 |
    sample_rate_hz:u32 | channels:u8 | payload_length:u32 | crc32:u32 | PCM16

CRC32 covers the complete header before the CRC field followed by the payload.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass


MAGIC = b"CAUD"
VERSION = 1
SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
MAX_PAYLOAD_BYTES = 65_536
HEADER_WITHOUT_CRC = struct.Struct(">4sBIQIBI")
HEADER = struct.Struct(">4sBIQIBII")


@dataclass(frozen=True, slots=True)
class AudioFrame:
    sequence: int
    timestamp_us: int
    sample_rate_hz: int
    channels: int
    pcm16: bytes

    @property
    def sample_count(self) -> int:
        return len(self.pcm16) // (BYTES_PER_SAMPLE * self.channels)


def _validate_frame_values(
    pcm16: bytes,
    sequence: int,
    timestamp_us: int,
    sample_rate_hz: int,
    channels: int,
    max_payload_bytes: int = MAX_PAYLOAD_BYTES,
) -> None:
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 <= sequence <= 0xFFFFFFFF
    ):
        raise ValueError("sequence must fit an unsigned 32-bit integer")
    if (
        isinstance(timestamp_us, bool)
        or not isinstance(timestamp_us, int)
        or not 0 <= timestamp_us <= 0xFFFFFFFFFFFFFFFF
    ):
        raise ValueError("timestamp_us must fit an unsigned 64-bit integer")
    if sample_rate_hz != SAMPLE_RATE_HZ or channels != CHANNELS:
        raise ValueError("audio must be 16 kHz mono PCM16")
    if not pcm16 or len(pcm16) > max_payload_bytes or len(pcm16) % BYTES_PER_SAMPLE:
        raise ValueError("PCM16 payload must be non-empty, even-sized, and within the size limit")


def encode_audio_frame(
    pcm16: bytes | bytearray | memoryview,
    *,
    sequence: int,
    timestamp_us: int,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    channels: int = CHANNELS,
) -> bytes:
    """Encode a version-1 frame (primarily useful in STM32/host integration tests)."""

    if not isinstance(pcm16, (bytes, bytearray, memoryview)):
        raise TypeError("pcm16 must be bytes-like")
    payload = bytes(pcm16)
    _validate_frame_values(payload, sequence, timestamp_us, sample_rate_hz, channels)
    header_without_crc = HEADER_WITHOUT_CRC.pack(
        MAGIC,
        VERSION,
        sequence,
        timestamp_us,
        sample_rate_hz,
        channels,
        len(payload),
    )
    checksum = binascii.crc32(header_without_crc + payload) & 0xFFFFFFFF
    return header_without_crc + struct.pack(">I", checksum) + payload


class AudioStreamDecoder:
    """Incremental USART decoder with bounded payloads and magic-based resync."""

    __slots__ = (
        "_buffer",
        "max_payload_bytes",
        "crc_errors",
        "invalid_headers",
        "bytes_discarded",
    )

    def __init__(self, *, max_payload_bytes: int = MAX_PAYLOAD_BYTES) -> None:
        if (
            isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_payload_bytes <= 0
            or max_payload_bytes > MAX_PAYLOAD_BYTES
        ):
            raise ValueError(f"max_payload_bytes must be in [1, {MAX_PAYLOAD_BYTES}]")
        self._buffer = bytearray()
        self.max_payload_bytes = max_payload_bytes
        self.crc_errors = 0
        self.invalid_headers = 0
        self.bytes_discarded = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self.bytes_discarded += len(self._buffer)
        self._buffer.clear()

    def _seek_magic(self) -> bool:
        index = self._buffer.find(MAGIC)
        if index == 0:
            return True
        if index > 0:
            del self._buffer[:index]
            self.bytes_discarded += index
            return True

        retained = min(len(self._buffer), len(MAGIC) - 1)
        discarded = len(self._buffer) - retained
        if discarded:
            del self._buffer[:discarded]
            self.bytes_discarded += discarded
        return False

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[AudioFrame, ...]:
        """Append arbitrary serial bytes and return every complete valid frame."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        self._buffer.extend(data)
        frames: list[AudioFrame] = []

        while True:
            if not self._seek_magic() or len(self._buffer) < HEADER.size:
                break
            try:
                (
                    magic,
                    version,
                    sequence,
                    timestamp_us,
                    sample_rate_hz,
                    channels,
                    payload_length,
                    expected_crc,
                ) = HEADER.unpack_from(self._buffer)
            except struct.error:
                break

            header_valid = (
                magic == MAGIC
                and version == VERSION
                and sample_rate_hz == SAMPLE_RATE_HZ
                and channels == CHANNELS
                and 0 < payload_length <= self.max_payload_bytes
                and payload_length % BYTES_PER_SAMPLE == 0
            )
            if not header_valid:
                del self._buffer[0]
                self.invalid_headers += 1
                self.bytes_discarded += 1
                continue

            frame_length = HEADER.size + payload_length
            if len(self._buffer) < frame_length:
                break
            payload = bytes(self._buffer[HEADER.size : frame_length])
            actual_crc = binascii.crc32(bytes(self._buffer[: HEADER_WITHOUT_CRC.size]) + payload)
            actual_crc &= 0xFFFFFFFF
            if actual_crc != expected_crc:
                del self._buffer[0]
                self.crc_errors += 1
                self.bytes_discarded += 1
                continue

            frames.append(
                AudioFrame(
                    sequence=sequence,
                    timestamp_us=timestamp_us,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                    pcm16=payload,
                )
            )
            del self._buffer[:frame_length]

        return tuple(frames)


__all__ = [
    "AudioFrame",
    "AudioStreamDecoder",
    "BYTES_PER_SAMPLE",
    "CHANNELS",
    "HEADER",
    "MAGIC",
    "MAX_PAYLOAD_BYTES",
    "SAMPLE_RATE_HZ",
    "VERSION",
    "encode_audio_frame",
]
