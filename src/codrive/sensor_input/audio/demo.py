"""Replay an AI Hub WAV through the real rolling classifier and optional DDS/RViz outputs."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ...contracts.models import to_json
from .dataset import ArchiveAudioStore
from .features import decode_wav, fit_window
from .manifest import read_manifest
from .model import load_audio_backend
from .stream import Stm32AudioInferenceAdapter, encode_waveform_as_stm32_frames
from .worker import RollingAudioWorker, TemporalEventFilter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--label", choices=("siren", "horn", "background"), default="siren")
    parser.add_argument(
        "--partition", choices=("train", "tune", "final_test"), default="final_test"
    )
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--rviz", action="store_true")
    parser.add_argument("--dds", action="store_true")
    parser.add_argument("--domain-id", type=int, default=42)
    args = parser.parse_args(argv)
    if args.index < 0 or args.chunk_ms <= 0:
        parser.error("index must be non-negative and chunk-ms positive")

    candidates = [
        record
        for record in read_manifest(args.manifest)
        if record.partition == args.partition and record.label == args.label
    ]
    if args.index >= len(candidates):
        parser.error(f"index {args.index} is outside {len(candidates)} matching records")
    record = candidates[args.index]
    store = ArchiveAudioStore()
    try:
        waveform = decode_wav(store.read(record))
    finally:
        store.close()
    classifier = load_audio_backend(args.model, device=args.device)
    config = classifier.feature_config
    # One second of silence warms the rolling buffer and makes event onset clear.
    silence = np.zeros(config.sample_rate_hz, dtype=np.float32)
    event_audio = fit_window(waveform, max(config.window_samples, waveform.size))
    stream = np.concatenate((silence, event_audio, silence))
    siren_threshold, horn_threshold = (float(value) for value in classifier.thresholds)
    off_threshold = min(0.42, siren_threshold - 0.05, horn_threshold - 0.05)
    worker = RollingAudioWorker(
        classifier,
        feature_config=config,
        event_filter=TemporalEventFilter(
            siren_on_threshold=siren_threshold,
            horn_on_threshold=horn_threshold,
            off_threshold=off_threshold,
        ),
    )
    serial_adapter = Stm32AudioInferenceAdapter(worker)

    visualizer = None
    if args.rviz:
        from .rviz import RosAudioVisualizer

        visualizer = RosAudioVisualizer(
            siren_threshold=siren_threshold,
            horn_threshold=horn_threshold,
        )
    bus = publisher = None
    if args.dds:
        from ...apps.jetson_publisher import JetsonPerceptionPublisher
        from ...transport import CycloneDdsBus

        bus = CycloneDdsBus(domain_id=args.domain_id)
        publisher = JetsonPerceptionPublisher(bus, "jetson-cabin")

    print(
        json.dumps(
            {
                "selected": record.decoded_member,
                "label": record.label,
                "duration_s": waveform.size / config.sample_rate_hz,
                "model": classifier.model_version,
                "thresholds": {
                    "siren": siren_threshold,
                    "horn": horn_threshold,
                    "off": off_threshold,
                },
            },
            ensure_ascii=False,
        )
    )
    encoded_frames = encode_waveform_as_stm32_frames(stream, frame_ms=args.chunk_ms)
    next_tick = time.monotonic()
    try:
        for serial_frame in encoded_frames:
            for result in serial_adapter.feed_serial(serial_frame, received_time_ns=time.time_ns()):
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
                if publisher is not None:
                    publisher.audio(result.event)
                if visualizer is not None:
                    visualizer.publish(result)
            if args.realtime:
                next_tick += args.chunk_ms / 1_000.0
                remaining = next_tick - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        print(json.dumps({"serial_stats": asdict(serial_adapter.stats)}, ensure_ascii=False))
    finally:
        if visualizer is not None:
            visualizer.close()
        if bus is not None:
            bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
