"""Offline rule scenarios requiring neither sensor nodes nor a DDS installation."""

from __future__ import annotations

import argparse
import time
from dataclasses import replace

from ..contracts.models import (
    AudioEvent, AudioKind, DriverState, ExternalPerception, Header,
    TrafficLight, VehicleState, to_json,
)
from ..apps.raspberry_pi.pipeline import DecisionPipeline, PipelineResult
from ..risk import RiskEngine, RiskPolicy, load_risk_policy


def run_rule_scenarios(policy: RiskPolicy | None = None) -> list[tuple[str, PipelineResult]]:
    now_ns = time.time_ns()

    def header(source: str) -> Header:
        return Header.now(source, 1, capture_time_ns=now_ns)

    vehicle = VehicleState(header("mock.vehicle"), 40 / 3.6, 0.0, False, 18.0)
    driver = DriverState(header("mock.driver"), "cabin", "mock", 0.95, True)
    external = ExternalPerception(header("mock.external"), "front", "mock")
    audio = AudioEvent(header("mock.audio"), AudioKind.SIREN, 0.91, 960, "mock", "rear")
    cases = [
        ("normal", (vehicle, driver, external)),
        ("phone_use", (vehicle, replace(driver, phone_detected=True), external)),
        ("red_light_stopped", (replace(vehicle, speed_mps=0, brake_pressed=True), driver,
                                replace(external, traffic_light=TrafficLight.RED,
                                        traffic_light_confidence=0.95))),
        ("red_light_moving", (vehicle, driver,
                               replace(external, traffic_light=TrafficLight.RED,
                                       traffic_light_confidence=0.95))),
        ("siren_stopped", (replace(vehicle, speed_mps=0), driver, external, audio)),
        ("siren_fast_rear", (replace(vehicle, speed_mps=60 / 3.6), driver, external, audio)),
        ("multiple_risks", (vehicle, replace(driver, phone_detected=True), audio,
                            replace(external, traffic_light=TrafficLight.RED))),
        ("can_missing", (driver, replace(external, traffic_light=TrafficLight.RED))),
    ]
    results = []
    for name, samples in cases:
        pipeline = DecisionPipeline(risk_engine=RiskEngine(policy=policy or RiskPolicy()))
        for sample in samples:
            pipeline.ingest(sample, received_time_ns=now_ns)
        results.append((name, pipeline.step(now_ns=now_ns)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", help="Path to a risk policy YAML file")
    parser.add_argument("--details", action="store_true", help="Print complete decision results")
    args = parser.parse_args()
    policy = load_risk_policy(args.policy) if args.policy else RiskPolicy()
    for name, result in run_rule_scenarios(policy):
        if args.details:
            print(to_json({"scenario": name, "result": result}))
            continue
        assessment = result.assessment
        selected = assessment.selected_risk
        print(to_json({
            "scenario": name, "level": assessment.stable_level,
            "risk": selected.code if selected else None,
            "score": selected.score if selected else 0,
            "action": selected.recommended_action if selected else None,
            "degraded": assessment.degraded,
            "candidates": [{"code": item.code, "score": item.score, "level": item.level}
                           for item in assessment.candidates],
        }))


if __name__ == "__main__":
    main()
