"""Isolated AudioEvent -> Pi HMI validation; never publish to live domain 42."""
import argparse
import os
import time
from pathlib import Path

from codrive.models import AudioEvent, AudioKind, Header, NodeHealth, NodeState
from codrive.runtime import publish_model
from codrive.transport import CycloneDdsBus

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--domain-id", type=int, default=44)
parser.add_argument("--dds-config", required=True)
args = parser.parse_args()
if args.domain_id == 42:
    parser.error("Synthetic samples must never enter the live domain")
os.environ["CYCLONEDDS_URI"] = Path(args.dds_config).resolve().as_uri()
bus = CycloneDdsBus(domain_id=args.domain_id)
started = time.monotonic()
sequence = 0
try:
    while time.monotonic() - started < 8:
        sequence += 1
        siren = time.monotonic() - started < 4
        publish_model(bus, "audio_event", AudioEvent(
            Header.now("jetson-cabin.audio", sequence),
            AudioKind.SIREN if siren else AudioKind.NONE, 0.99, 960, "TEST_FIXTURE",
        ))
        publish_model(bus, "node_health", NodeHealth(
            Header.now("jetson-cabin.audio", sequence), "jetson-cabin.audio",
            NodeState.READY, time.monotonic() - started, detail="TEST_FIXTURE",
        ))
        time.sleep(0.1)
finally:
    bus.close()
