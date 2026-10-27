"""Run the Raspberry Pi DDS fusion/risk/guidance orchestrator."""

from __future__ import annotations

import argparse
import signal
from threading import Event

from ...hmi.web import HmiWebConfig, HmiWebServer
from ...monitoring.health import HealthPolicy, NodeHealthMonitor
from ...risk import RiskEngine, load_risk_policy
from ...transport import CycloneDdsBus
from .runtime import PiOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain-id", type=int, default=42)
    parser.add_argument("--rate-hz", type=float, default=50.0)
    parser.add_argument("--risk-policy", default="config/risk-legacy.yaml")
    parser.add_argument("--hmi-web-host", default="0.0.0.0")
    parser.add_argument("--hmi-web-port", type=int, default=8090)
    parser.add_argument("--hmi-vehicle-stale-ms", type=int, default=500)
    parser.add_argument("--no-hmi-web", action="store_true")
    args = parser.parse_args()

    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    health = NodeHealthMonitor(
        HealthPolicy(expected_nodes=("jetson-front", "jetson-cabin"))
    )
    risk = RiskEngine(policy=load_risk_policy(args.risk_policy))
    hmi_web = None
    if not args.no_hmi_web:
        hmi_web = HmiWebServer(
            HmiWebConfig(
                host=args.hmi_web_host,
                port=args.hmi_web_port,
                stale_after_ms=args.hmi_vehicle_stale_ms,
            )
        )
        hmi_web.start()
        print(f"CODRIVE HMI: {hmi_web.url}", flush=True)

    try:
        with CycloneDdsBus(domain_id=args.domain_id) as bus:
            PiOrchestrator(
                bus,
                risk_engine=risk,
                health_monitor=health,
                snapshot_sink=hmi_web,
            ).run(stop, rate_hz=args.rate_hz)
    finally:
        if hmi_web is not None:
            hmi_web.close()


if __name__ == "__main__":
    main()
