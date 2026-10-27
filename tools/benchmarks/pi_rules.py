"""Offline Pi rules benchmark; run with PYTHONPATH=board_runtime/src.

No API calls, DDS participants, image inference or audio devices are opened.
Results describe the host running this script, not a guaranteed Pi latency.
"""

import argparse
import json
import platform
import statistics
import time
from dataclasses import replace

from codrive.fusion import FusionPolicy, SituationFusion
from codrive.models import (
    AudioEvent,
    AudioKind,
    BoundingBox,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    SituationSnapshot,
    TrackedObject,
    TrafficLight,
    VehicleState,
    Weather,
    to_json,
)

from codrive.v3_feature_engine import RiskEngine, RiskPolicy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--iterations", type=int, default=10000)
args = parser.parse_args()
if args.iterations < 100:
    parser.error("--iterations must be at least 100")
n = args.iterations
base = time.time_ns() - int((n + 1000) * 20_000_000)
h = Header.now("bench", 1, capture_time_ns=base)
objects = tuple(
    TrackedObject(
        i, "car" if i % 2 else "pedestrian", 0.9, BoundingBox(0.2, 0.2, 0.8, 0.9)
    )
    for i in range(30)
)
vehicle = VehicleState(h, 12, 20, False, 40)
driver = DriverState(h, "cabin", "bench", 0.3, False, behavior="distracted")
audio = AudioEvent(h, AudioKind.SIREN, 0.94, 960, "bench", "left")
external = ExternalPerception(
    h, "front", "bench", objects=objects, traffic_light=TrafficLight.RED
)
environment = EnvironmentState(h, weather=Weather.RAIN, is_night=True, api_valid=True)
snapshot = SituationSnapshot(
    h,
    vehicle=vehicle,
    driver=driver,
    audio=audio,
    external=(external,),
    environment=environment,
)


def measure(name, callback):
    for i in range(500):
        callback(i)
    elapsed = []
    cpu_start = time.process_time_ns()
    for i in range(n):
        start = time.perf_counter_ns()
        callback(i + 500)
        elapsed.append((time.perf_counter_ns() - start) / 1000)
    cpu_per_us = (time.process_time_ns() - cpu_start) / n / 1000
    ordered = sorted(elapsed)
    print(
        json.dumps(
            {
                "name": name,
                "iterations": n,
                "p50_us": round(statistics.median(elapsed), 2),
                "p95_us": round(ordered[int(n * 0.95)], 2),
                "p99_us": round(ordered[int(n * 0.99)], 2),
                "cpu_us_per_cycle": round(cpu_per_us, 2),
                "estimated_one_core_percent_at_50hz": round(cpu_per_us * 50 / 10000, 3),
            }
        )
    )


engine = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
measure("risk_only_current_rules", lambda i: engine.evaluate(snapshot, now_ns=base))

# Approximate local fusion/risk/snapshot serialization, excluding DDS and HTTP.
# Incoming values are refreshed, with independent real stream identities.
fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
engine = RiskEngine(policy=RiskPolicy(cabin_observation_enabled=True))
samples = (vehicle, driver, audio, external, environment)


def local_cycle(i):
    now = base + i * 20_000_000
    for j, sample in enumerate(samples):
        header = replace(
            h,
            source_id="bench." + str(j),
            sequence=i + 1,
            capture_time_ns=now,
            publish_time_ns=now,
        )
        fusion.ingest(replace(sample, header=header), received_time_ns=now)
    fused = fusion.snapshot(now_ns=now)
    engine.evaluate(fused, now_ns=now)
    json.loads(to_json(fused))


measure("fusion_risk_snapshot_json_30_objects", local_cycle)
measure("read_device_time", lambda i: time.time_ns())
print(
    json.dumps(
        {
            "host_arch": platform.machine(),
            "python": platform.python_version(),
            "note": "measured on the reported host; excludes image decoding, DDS, HTTP, and API calls",
        }
    )
)
