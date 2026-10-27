"""Run Jetson microphone/WAV/STM32 input, diagnostics and waveform inference."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from .. import latency_probe as latency
from ..config import load_config
from ..models import Header, NodeHealth, NodeState, to_json
from ..hardware.audio_input import SerialAudio, alsa_chunks, wav_chunks
from ..hardware.h723_audio import H723Audio
from ..sensor_input.audio.classifier import RollingAudio, WaveformClassifier
from ..sensor_input.audio.logmel import LogMelClassifier
from .local_bus import LocalBus
from ..fusion import SituationFusion
from ..risk import RiskEngine
from ..runtime import publish_model


def run_audio(
    chunks,
    *,
    bus,
    classifier=None,
    window_ms=960,
    hop_ms=100,
    max_windows=None,
    source_id="jetson-cabin.audio",
    node_id="jetson-cabin.audio",
    print_json=False,
    local_risk=False,
    heartbeat_hz=2.0,
    replay=False,
    source_stats=None,
    diagnostics=False,
):
    if max_windows is not None and (isinstance(max_windows, bool) or max_windows < 1):
        raise ValueError("max_windows must be positive")
    if not math.isfinite(heartbeat_hz) or heartbeat_hz <= 0:
        raise ValueError("heartbeat_hz must be positive and finite")
    rolling = RollingAudio(window_ms, hop_ms)
    pipeline = SituationFusion() if local_risk else None
    risk_engine = RiskEngine() if local_risk else None
    started, next_health, health_sequence, count, dropped = time.monotonic(), 0, 0, 0, 0
    iterator = iter(chunks)

    def health(state, detail):
        nonlocal health_sequence
        health_sequence += 1
        stats = source_stats() if source_stats else {}
        total_dropped = (
            max(dropped, stats.get("sequence_gaps", 0))
            + stats.get("crc_errors", 0)
            + stats.get("duplicates", 0)
            + stats.get("host_queue_drops", 0)
        )
        if stats:
            detail += ":" + json.dumps(stats, separators=(",", ":"))
        publish_model(
            bus,
            "node_health",
            NodeHealth(
                Header.now(node_id, health_sequence),
                node_id,
                state,
                time.monotonic() - started,
                dropped_samples=total_dropped,
                detail=detail,
            ),
        )

    try:
        health(NodeState.STARTING, "audio_starting")
        for chunk in iterator:
            start_ns = chunk.end_ns - len(chunk.pcm16) // 2 * 62500
            gap = chunk.discontinuity or (
                rolling.end_ns is not None and abs(start_ns - rolling.end_ns) > 2_000_000
            )
            if gap and classifier and hasattr(classifier, "reset"):
                classifier.reset()
            if gap:
                dropped += 1
                health(NodeState.DEGRADED, "audio_stream_discontinuity")
            if time.monotonic() >= next_health:
                health(
                    NodeState.READY if classifier else NodeState.DEGRADED,
                    "audio_running" if classifier else "capture_only:no_classifier",
                )
                next_health = time.monotonic() + 1 / heartbeat_hz
            for window in rolling.feed(chunk.pcm16, chunk.end_ns, discontinuity=gap):
                count += 1
                base_header = Header.now(source_id, count, frame_id="microphone")
                header = replace(
                    base_header,
                    capture_time_ns=window.capture_time_ns,
                    publish_time_ns=max(base_header.publish_time_ns, window.capture_time_ns),
                )
                if classifier:
                    trace=latency.begin("audio_window")
                    event = classifier.classify(window, header)
                    event = replace(
                        event,
                        header=replace(
                            event.header,
                            publish_time_ns=max(time.time_ns(), window.capture_time_ns),
                        ),
                    )
                    publish_model(bus, "audio_event", event)
                    if trace is not None:
                        latency.link(frame=latency.header_key(event.header),window_end_ns=window.capture_time_ns,
                                     window_ms=window.window_ms)
                        latency.finish(trace)
                    if print_json:
                        print(to_json(event), flush=True)
                    if diagnostics and hasattr(classifier, "last_probabilities"):
                        siren, horn = classifier.last_probabilities
                        print(
                            json.dumps(
                                {
                                    "type": "audio_diagnostics",
                                    "sequence": count,
                                    "siren_probability": siren,
                                    "horn_probability": horn,
                                    "inference_ms": classifier.inference_ms,
                                }
                            ),
                            flush=True,
                        )
                    if pipeline:
                        now_ns = window.capture_time_ns if replay else time.time_ns()
                        pipeline.ingest(event, received_time_ns=now_ns)
                        snapshot = pipeline.snapshot(now_ns=now_ns)
                        risk = risk_engine.evaluate(snapshot, now_ns=now_ns)
                        print(to_json(risk or snapshot), flush=True)
                elif print_json:
                    samples = np.frombuffer(window.pcm16, dtype="<i2").astype(np.float32) / 32768
                    print(
                        json.dumps(
                            {
                                "mode": "capture_only",
                                "sequence": count,
                                "capture_time_ns": window.capture_time_ns,
                                "window_ms": window.window_ms,
                                "rms": float(np.sqrt(np.mean(samples**2))),
                                "peak": float(np.max(np.abs(samples))),
                            }
                        ),
                        flush=True,
                    )
                if max_windows is not None and count >= max_windows:
                    return count
        if count == 0:
            raise ValueError("Input did not contain a complete audio window")
        return count
    except Exception:
        health(NodeState.FAILED, "audio_execution_failed")
        raise
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
        health(NodeState.DEGRADED, "audio_stopped")


def main(argv=None):
    latency.install_audio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/board/jetson-cabin.yaml")
    parser.add_argument("--input", choices=("alsa", "wav", "stm32", "h723"))
    parser.add_argument("--wav")
    parser.add_argument("--device", help="ALSA capture name or STM32 serial path")
    parser.add_argument("--baudrate", type=int, help="Serial baudrate override")
    parser.add_argument("--channel", choices=("left", "right", "mean"), help="H723 input channel")
    parser.add_argument(
        "--capture-only", action="store_true", help="PCM diagnostics only; never emits AudioEvent"
    )
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=("checkpoint", "torchscript", "tensorrt"))
    parser.add_argument("--inference-device", default="cuda:0")
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--dry-run", action="store_true", help="Local transport instead of DDS")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--local-risk", action="store_true")
    parser.add_argument(
        "--siren-only", action="store_true", help="Monitor horn probability without activating HORN"
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Print checkpoint probabilities and feature+inference latency",
    )
    args = parser.parse_args(argv)
    if args.baudrate is not None and args.baudrate <= 0:
        parser.error("--baudrate must be positive")
    path = Path(args.config).resolve()
    config = load_config(path)
    audio = config["audio"]
    if not audio.get("enabled", True):
        parser.error("Audio is disabled in the profile")
    if args.capture_only and args.local_risk:
        parser.error("capture-only has no classification; local-risk requires a model")
    timeout = audio.get("input_timeout_s", 3.0)
    if not math.isfinite(timeout) or timeout <= 0:
        parser.error("input_timeout_s must be positive and finite")
    backend = args.backend or audio["backend"]
    classifier = None
    if args.siren_only and backend != "checkpoint":
        parser.error("--siren-only requires the yyj checkpoint backend")
    if not args.capture_only:
        model = Path(args.model or audio["engine"])
        if not model.is_absolute():
            model = (Path.cwd() if args.model else path.parents[2]) / model
        if backend == "checkpoint":
            classifier = LogMelClassifier(
                model,
                device=args.inference_device,
                window_ms=audio["rolling_window_ms"],
                enable_horn=not args.siren_only and audio.get("enable_horn", True),
            )
        else:
            classifier = WaveformClassifier(
                model,
                backend=backend,
                device=args.inference_device,
                labels=audio.get("labels", ["none", "siren", "horn", "other"]),
                output=audio.get("output", "logits"),
                threshold=audio.get("confidence_threshold", 0.7),
                window_ms=audio["rolling_window_ms"],
            )
    mode = args.input or ("wav" if args.wav else audio.get("input", "stm32"))
    if mode == "wav" and not args.dry_run:
        parser.error(
            "WAV replay requires --dry-run; its virtual sample clock must not enter live DDS"
        )
    if mode == "wav":
        if not args.wav:
            parser.error("--input wav requires --wav")
        chunks = wav_chunks(args.wav)
    elif mode == "alsa":
        chunks = alsa_chunks(
            args.device or audio.get("alsa_device", "plughw:CARD=Webcam,DEV=0"), timeout
        )
    elif mode == "h723":
        h723 = audio.get("h723", {})
        receiver = H723Audio(
            args.device or h723.get("device", "auto"),
            args.baudrate or h723.get("baudrate", 3000000), timeout,
            channel=args.channel or h723.get("channel", "left"),
            startup_drain_s=h723.get("startup_drain_s", 0.5),
        )
        chunks = receiver.chunks()
    else:
        receiver = SerialAudio(args.device or audio.get("device"),
                               args.baudrate or audio["baudrate"], timeout)
        chunks = receiver.chunks()
    if args.dry_run:
        bus = LocalBus()
    else:
        if "CYCLONEDDS_URI" not in os.environ:
            uri = config["dds"].get("config_uri")
            if uri:
                os.environ["CYCLONEDDS_URI"] = (path.parents[2] / uri).resolve().as_uri()
        from ..transport import CycloneDdsBus

        bus = CycloneDdsBus(domain_id=config["dds"]["domain_id"])
    try:
        run_audio(
            chunks,
            bus=bus,
            classifier=classifier,
            window_ms=audio["rolling_window_ms"],
            hop_ms=audio["hop_ms"],
            max_windows=args.max_windows,
            source_id=audio["source_id"],
            node_id=f"{config['node_id']}.audio",
            print_json=args.print_json,
            local_risk=args.local_risk,
            heartbeat_hz=config["health"]["heartbeat_hz"],
            replay=mode == "wav",
            source_stats=receiver.stats if mode in ("stm32", "h723") else None,
            diagnostics=args.diagnostics,
        )
    except KeyboardInterrupt:
        return 130
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
