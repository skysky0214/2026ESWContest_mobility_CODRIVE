"""Deterministic end-to-end demonstration of the local safety path."""

from __future__ import annotations

import time

from .fusion import FusionPolicy, SituationFusion
from .guidance import HmiPolicy
from .models import ExternalPerception, Header, TrafficLight, VehicleState, to_json
from .risk import RiskEngine


def run_red_light_scenario() -> tuple[str, float]:
    scenario_time_ns = time.time_ns()
    fusion = SituationFusion(policy=FusionPolicy(expected_camera_ids=("front",)))
    fusion.ingest(
        VehicleState(
            header=Header.now("sim.panda", 1, capture_time_ns=scenario_time_ns),
            speed_mps=40 / 3.6,
            steering_wheel_deg=0.0,
            brake_pressed=False,
            accelerator_pct=15.0,
        ),
        received_time_ns=scenario_time_ns,
    )
    fusion.ingest(
        ExternalPerception(
            header=Header.now("sim.jetson-front", 1, capture_time_ns=scenario_time_ns),
            camera_id="front",
            model_version="sim",
            traffic_light=TrafficLight.RED,
        ),
        received_time_ns=scenario_time_ns,
    )

    started_ns = time.perf_counter_ns()
    snapshot = fusion.snapshot(now_ns=scenario_time_ns)
    event = RiskEngine().evaluate(snapshot, now_ns=scenario_time_ns)
    if event is None:
        raise RuntimeError("scenario did not produce a risk event")
    command = HmiPolicy().render(event)
    latency_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    return to_json(command), latency_ms


def main() -> None:
    command_json, latency_ms = run_red_light_scenario()
    print(command_json)
    print(f"local fusion+risk+hmi latency: {latency_ms:.3f} ms")


if __name__ == "__main__":
    main()
