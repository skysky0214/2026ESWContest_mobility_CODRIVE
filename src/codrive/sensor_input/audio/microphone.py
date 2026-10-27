"""Run live siren/horn inference from an Ubuntu laptop microphone."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from ...contracts.models import AudioKind, to_json
from .model import load_audio_backend
from .worker import RollingAudioWorker, TemporalEventFilter


def build_arecord_command(
    capture_device: str,
    *,
    sample_rate_hz: int = 16_000,
) -> tuple[str, ...]:
    """Return a shell-free ALSA capture command producing mono PCM16 bytes."""

    if not capture_device:
        raise ValueError("capture_device must not be empty")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    return (
        "arecord",
        "-q",
        "-D",
        capture_device,
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-c",
        "1",
        "-r",
        str(sample_rate_hz),
    )


class ArecordPcmCapture:
    """Read fixed-duration PCM chunks from PulseAudio/PipeWire through ALSA."""

    def __init__(
        self,
        capture_device: str = "pulse",
        *,
        sample_rate_hz: int = 16_000,
        chunk_ms: int = 20,
    ) -> None:
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        self.capture_device = capture_device
        self.sample_rate_hz = sample_rate_hz
        self.chunk_ms = chunk_ms
        self.chunk_bytes = round(sample_rate_hz * chunk_ms / 1_000) * 2
        self.command = build_arecord_command(
            capture_device, sample_rate_hz=sample_rate_hz
        )
        self.process: subprocess.Popen[bytes] | None = None

    def open(self) -> "ArecordPcmCapture":
        if self.process is not None:
            return self
        if shutil.which("arecord") is None:
            raise RuntimeError("arecord is missing; install the alsa-utils package")
        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        return self

    def read(self) -> bytes:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("microphone capture is not open")
        payload = bytearray()
        while len(payload) < self.chunk_bytes:
            block = self.process.stdout.read(self.chunk_bytes - len(payload))
            if block:
                payload.extend(block)
                continue
            error = b""
            if self.process.stderr is not None:
                error = self.process.stderr.read()
            detail = error.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"arecord stopped: {detail or 'no PCM data'}")
        return bytes(payload)

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def __enter__(self) -> "ArecordPcmCapture":
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--capture-device",
        default="pulse",
        help="ALSA capture name; pulse is normally the Ubuntu selected input",
    )
    parser.add_argument("--compute-device", default="cpu", help="cpu, cuda, or cuda:0")
    parser.add_argument("--siren-threshold", type=float, help="override checkpoint threshold")
    parser.add_argument("--horn-threshold", type=float, help="override checkpoint threshold")
    parser.add_argument(
        "--siren-only",
        action="store_true",
        help="show horn probability but never activate a HORN event",
    )
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--duration-s", type=float, help="stop automatically after this duration")
    parser.add_argument("--status-interval-s", type=float, default=1.0)
    parser.add_argument("--rviz", action="store_true")
    parser.add_argument("--dds", action="store_true")
    parser.add_argument("--domain-id", type=int, default=42)
    args = parser.parse_args(argv)
    if args.chunk_ms <= 0 or args.status_interval_s <= 0:
        parser.error("chunk-ms and status-interval-s must be positive")
    if args.duration_s is not None and args.duration_s <= 0:
        parser.error("duration-s must be positive")

    for name, value in (
        ("siren-threshold", args.siren_threshold),
        ("horn-threshold", args.horn_threshold),
    ):
        if value is not None and not 0.05 < value <= 1.0:
            parser.error(f"{name} must be in (0.05, 1]")

    classifier = load_audio_backend(args.model, device=args.compute_device)
    checkpoint_siren, checkpoint_horn = (float(value) for value in classifier.thresholds)
    siren_threshold = (
        checkpoint_siren if args.siren_threshold is None else args.siren_threshold
    )
    horn_threshold = checkpoint_horn if args.horn_threshold is None else args.horn_threshold
    off_threshold = min(0.42, siren_threshold - 0.05, horn_threshold - 0.05)
    worker = RollingAudioWorker(
        classifier,
        source_id="laptop.audio",
        frame_id="laptop_microphone",
        feature_config=classifier.feature_config,
        event_filter=TemporalEventFilter(
            siren_on_threshold=siren_threshold,
            horn_on_threshold=horn_threshold,
            off_threshold=off_threshold,
            enable_horn=not args.siren_only,
        ),
    )

    visualizer = None
    if args.rviz:
        from .rviz import RosAudioVisualizer

        visualizer = RosAudioVisualizer(
            siren_threshold=siren_threshold,
            horn_threshold=horn_threshold,
            horn_enabled=not args.siren_only,
        )
    bus = publisher = None
    if args.dds:
        from ...apps.jetson_publisher import JetsonPerceptionPublisher
        from ...transport import CycloneDdsBus

        bus = CycloneDdsBus(domain_id=args.domain_id)
        publisher = JetsonPerceptionPublisher(bus, "jetson-cabin")

    capture = ArecordPcmCapture(
        args.capture_device,
        sample_rate_hz=classifier.feature_config.sample_rate_hz,
        chunk_ms=args.chunk_ms,
    )
    print(
        json.dumps(
            {
                "capture_device": args.capture_device,
                "sample_rate_hz": classifier.feature_config.sample_rate_hz,
                "model": classifier.model_version,
                "thresholds": {"siren": siren_threshold, "horn": horn_threshold},
                "siren_only": args.siren_only,
                "message": "listening; press Ctrl+C to stop",
            },
            ensure_ascii=False,
        )
    )
    started = time.monotonic()
    next_status = started
    last_kind: AudioKind | None = None
    try:
        with capture:
            while args.duration_s is None or time.monotonic() - started < args.duration_s:
                payload = capture.read()
                for result in worker.feed_pcm16(payload, capture_time_ns=time.time_ns()):
                    if publisher is not None:
                        publisher.audio(result.event)
                    if visualizer is not None:
                        visualizer.publish(result)
                    now = time.monotonic()
                    if result.event.kind is not last_kind or now >= next_status:
                        print(
                            json.dumps(
                                {
                                    "event": json.loads(to_json(result.event)),
                                    "siren_probability": result.siren_probability,
                                    "horn_probability": result.horn_probability,
                                    "inference_ms": result.inference_ms,
                                },
                                ensure_ascii=False,
                            )
                        )
                        last_kind = result.event.kind
                        next_status = now + args.status_interval_s
    except KeyboardInterrupt:
        print("microphone capture stopped")
    finally:
        capture.close()
        if visualizer is not None:
            visualizer.close()
        if bus is not None:
            bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ArecordPcmCapture", "build_arecord_command", "main"]
