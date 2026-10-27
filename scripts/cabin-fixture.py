"""Bounded synthetic validation. Refuses to publish into live DDS domain 42."""
import argparse
import os
import time
from pathlib import Path

from codrive.models import DriverState, Header, NodeHealth, NodeState
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
        phone = time.monotonic() - started < 3
        face = time.monotonic() - started < 6
        publish_model(bus, "driver_state", DriverState(
            Header.now("jetson-cabin.dms", sequence), "cabin", "TEST_FIXTURE",
            0.95 if not phone else 0.1, not phone, phone_detected=phone,
            face_detected=face,
        ))
        publish_model(bus, "node_health", NodeHealth(
            Header.now("jetson-cabin", sequence), "jetson-cabin", NodeState.READY if face else NodeState.DEGRADED,
            time.monotonic() - started, detail="TEST_FIXTURE",
        ))
        time.sleep(0.0667)
finally:
    bus.close()
