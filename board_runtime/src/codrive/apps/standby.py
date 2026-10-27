"""Communication-only DDS health publisher/receiver without camera access."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import time
from pathlib import Path
from threading import Event

from ..config import load_config
from ..health import HealthPolicy, NodeHealthMonitor
from ..models import Header, NodeHealth, NodeState
from ..runtime import decode_model, publish_model
from ..transport import CycloneDdsBus

LOG = logging.getLogger("codrive.standby")


def run_standby(bus, *, stop=None, duration=None, heartbeat_hz=2,
                node_id="jetson-front", expected_peers=("pi5.hmi", "jetson-cabin")):
    if not math.isfinite(heartbeat_hz) or heartbeat_hz <= 0:
        raise ValueError("heartbeat_hz must be positive and finite")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be positive and finite")
    stop = stop or Event()
    monitor = NodeHealthMonitor(HealthPolicy(expected_nodes=tuple(expected_peers)))
    started = time.monotonic()
    sequence = received = rejected = 0
    next_health = started
    try:
        while not stop.is_set():
            for wire in bus.take_json("node_health", 128):
                try:
                    sample = decode_model(wire, NodeHealth)
                    if sample.node_id in expected_peers:
                        if monitor.ingest(sample):
                            received += 1
                        else:
                            rejected += 1
                except (ValueError, TypeError):
                    rejected += 1
            now = time.monotonic()
            if now >= next_health:
                sequence += 1
                peers = monitor.statuses()
                detail = json.dumps({
                    "mode": "communication_only", "camera_enabled": False,
                    "peers": {name: value["connected"] for name, value in peers.items()},
                    "received": received, "rejected": rejected,
                }, separators=(",", ":"))
                publish_model(bus, "node_health", NodeHealth(
                    Header.now(node_id, sequence), node_id, NodeState.DEGRADED,
                    now - started, detail=detail,
                ))
                LOG.info("standby camera=disabled peers=%s received=%d rejected=%d",
                         {name: value["connected"] for name, value in peers.items()},
                         received, rejected)
                next_health = now + 1 / heartbeat_hz
            if duration is not None and now - started >= duration:
                break
            stop.wait(0.02)
    finally:
        publish_model(bus, "node_health", NodeHealth(
            Header.now(node_id, sequence + 1), node_id, NodeState.DEGRADED,
            time.monotonic() - started, detail="standby_stopped:camera_disabled",
        ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/integration/jetson-front.yaml")
    parser.add_argument("--duration", type=float)
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    if config.get("camera_enabled") is not False:
        parser.error("Standby requires camera_enabled: false")
    os.environ["CYCLONEDDS_URI"] = (
        config_path.parents[2] / config["dds"]["config_uri"]
    ).resolve().as_uri()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    bus = CycloneDdsBus(domain_id=config["dds"]["domain_id"])
    try:
        run_standby(bus, stop=stop, duration=args.duration, node_id=config["node_id"],
                    heartbeat_hz=config["health"]["heartbeat_hz"],
                    expected_peers=config["health"]["expected_peers"])
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
