"""Offline timings of actual Pi processing stages using the saved sensor stream.

Run from the repository root with PYTHONPATH=board_runtime/src. No network,
hardware, DDS participant, HTTP server or speech playback is opened. The host
running this command is recorded; these are not automatically Raspberry Pi timings.
"""

from __future__ import annotations

import argparse
import gzip
import json
import platform
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
from codrive.apps.local_bus import LocalBus
from codrive.fusion import FusionPolicy, SituationFusion
from codrive.models import SituationSnapshot, from_json
from codrive.observation_speech import ObservationSpeechGate
from codrive.risk_ml.common import sha256, write_json
from codrive.risk_ml.features_v3 import probabilities, vector
from codrive.risk_ml.infer import Predictor
from codrive.runtime import PiOrchestrator, publish_model

from codrive.guidance import HmiArbiter
from codrive.v3_feature_engine import RiskEngine, RiskPolicy


def summarize(values):
    values = np.asarray(values, dtype=np.float64) / 1e6
    return {
        "samples": len(values),
        "mean_ms": float(values.mean()),
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "p99_ms": float(np.percentile(values, 99)),
        "max_ms": float(values.max()),
    }


class Stages:
    def __init__(self, predictor):
        self.predictor = predictor
        self.bus = LocalBus()
        self.orchestrator = PiOrchestrator(
            self.bus,
            fusion=SituationFusion(policy=FusionPolicy(environment_ttl_ms=3000)),
            risk_engine=RiskEngine(
                policy=RiskPolicy(**predictor.metadata["rule_policy"])
            ),
        )
        self.arbiter = HmiArbiter(repeat_speech_gap_ms=10_000)
        self.observations = ObservationSpeechGate()
        self.active_command = None

    def cycle(self, raw):
        now = raw.header.capture_time_ns
        # Simulated remote producers serialize before the measured receive path.
        for topic, value in (
            ("ins_state", raw.ins),
            ("driver_state", raw.driver),
            ("audio_event", raw.audio),
        ):
            if value is not None:
                publish_model(self.bus, topic, value)
        for perception in raw.external:
            publish_model(self.bus, "external_perception", perception)
        t0 = time.perf_counter_ns()
        # CAN and weather workers enter fusion locally, as in apps/pi.py.
        for value in (raw.vehicle, raw.environment):
            if value is not None:
                self.orchestrator.fusion.ingest(value, received_time_ns=now)
        self.orchestrator._receive_inputs(now)
        t1 = time.perf_counter_ns()
        snapshot = self.orchestrator.fusion.snapshot(now_ns=now)
        missing_nodes = self.orchestrator.health_monitor.missing_nodes(now_ns=now)
        if missing_nodes:
            snapshot = replace(
                snapshot,
                degraded_reasons=snapshot.degraded_reasons
                + tuple(f"heartbeat_missing:{node}" for node in missing_nodes),
            )
        t2 = time.perf_counter_ns()
        publish_model(self.bus, "situation_snapshot", snapshot)
        t3 = time.perf_counter_ns()
        engine = self.orchestrator.risk_engine
        risk = engine.evaluate(snapshot, now_ns=now)
        command = None
        if risk is not None:
            publish_model(self.bus, "risk_event", risk)
            command = self.orchestrator.hmi_policy.render(risk)
            publish_model(self.bus, "hmi_command", command)
        self.observations.update(snapshot.driver, now_ns=now)
        if command is not None:
            decision = self.arbiter.accept(
                command,
                now_ns=now,
                allow_speech=self.observations.eligible(command, now_ns=now),
            )
            if decision:
                self.active_command = decision.command
                if decision.speak:
                    self.observations.mark_spoken(decision.command)
        if self.active_command and self.active_command.expires_at_ns <= now:
            self.active_command = None
        t4 = time.perf_counter_ns()
        self.predictor.predict(engine.last_features)
        t5 = time.perf_counter_ns()
        # Separate booster-only timing includes the actual left/right symmetry policy.
        encoded = vector(engine.last_features, self.predictor.preprocessing)[None, :]
        t6 = time.perf_counter_ns()
        probabilities(self.predictor.model, encoded)
        t7 = time.perf_counter_ns()
        result = {
            "snapshot_integration_including_json": t3 - t0,
            "input_decode_and_fusion_ingest": t1 - t0,
            "snapshot_creation_only": t2 - t1,
            "snapshot_json_publication": t3 - t2,
            "xgboost_predictor_full": t5 - t4,
            "xgboost_booster_only": t7 - t6,
            "rule_risk_and_final_warning": t4 - t3,
        }
        if risk is not None:
            result["rule_risk_when_event_emitted"] = t4 - t3
        return result, risk.code if risk else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir", type=Path, default=Path("artifacts/models/risk/final-sensors-v3")
    )
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/benchmarks/risk-stages.json")
    )
    args = parser.parse_args()
    if args.iterations < 100 or args.warmup < 0:
        parser.error("--iterations >= 100 and --warmup >= 0 are required")
    stream_path = args.model_dir / "data/sensor_stream.jsonl.gz"
    with gzip.open(stream_path, "rt", encoding="utf-8") as handle:
        snapshots = [from_json(SituationSnapshot, line) for line in handle]
    if args.iterations > len(snapshots) or args.warmup > len(snapshots):
        parser.error("Iteration counts must not exceed the saved stream length")
    predictor = Predictor(args.model_dir)
    if predictor.contract_version != 3:
        parser.error("This benchmark requires the final sensor v3 model")
    print(f"Warming up {args.warmup} cycles...", flush=True)
    state = Stages(predictor)
    for raw in snapshots[: args.warmup]:
        state.cycle(raw)
    state = Stages(predictor)
    timings, event_codes = {}, Counter()
    print(f"Measuring {args.iterations} cycles...", flush=True)
    wall_started, cpu_started = time.perf_counter(), time.process_time()
    for raw in snapshots[: args.iterations]:
        elapsed, code = state.cycle(raw)
        for name, value in elapsed.items():
            timings.setdefault(name, []).append(value)
        if code:
            event_codes[code] += 1
    report = {
        "host": {
            "architecture": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "iterations": args.iterations,
        "warmup": args.warmup,
        "wall_seconds_including_fixture_preparation": time.perf_counter()
        - wall_started,
        "cpu_seconds_including_fixture_preparation": time.process_time() - cpu_started,
        "model_sha256": sha256(args.model_dir / "model.ubj"),
        "sensor_stream_sha256": sha256(stream_path),
        "stages": {name: summarize(values) for name, values in timings.items()},
        "emitted_event_codes": dict(event_codes),
        "rejected_samples": state.orchestrator.rejected_samples,
        "max_front_objects": max(
            (len(p.objects) for s in snapshots[: args.iterations] for p in s.external),
            default=0,
        ),
        "scope": {
            "snapshot_integration_including_json": "Local CAN/environment ingest + remote JSON decode/envelope validation + fusion + snapshot JSON publication on LocalBus",
            "xgboost_predictor_full": "36-field validation, 52-column encoding, CPU booster, left/right symmetric inference, class selection and warning templates; model loaded before timing",
            "xgboost_booster_only": "Already encoded 52-column row, including mirrored directional inference when applicable",
            "rule_risk_and_final_warning": "RiskEngine.evaluate (including shared feature extraction), repeat suppression, risk/HMI JSON publication, observation speech gate and HmiArbiter; no speech playback",
        },
        "limitations": [
            "Measured on the recorded host; not Raspberry Pi measurements unless run on the Pi",
            "Saved synthetic stream, at most one current packet per stream per cycle; not saturated DDS backlog",
            "LocalBus substitutes for DDS; excludes physical capture, edge inference, network transport, HTTP/browser rendering, weather API and audio playback",
            "Production XGBoost is asynchronous display-only at <=10Hz, not a serial input to the final warning decision",
            "No separate RiskStateMachine in board_runtime path; rule selection and HMI arbitration are timed instead",
            "Averages include cycles with no newly emitted event; emitted-event subset is reported separately",
        ],
    }
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
