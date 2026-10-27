"""Bidirectional DDS probe on an isolated domain (never the live domain 42)."""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from ..models import Header, NodeHealth, NodeState
from ..runtime import decode_model, publish_model
from ..transport import CycloneDdsBus


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("ping", "echo"))
    parser.add_argument("--dds-config", required=True)
    parser.add_argument("--domain-id", type=int, default=43)
    parser.add_argument("--duration", type=float, default=20)
    args = parser.parse_args(argv)
    if args.domain_id == 42:
        parser.error("probe data must not enter live domain 42")
    os.environ["CYCLONEDDS_URI"] = Path(args.dds_config).resolve().as_uri()
    bus = CycloneDdsBus(domain_id=args.domain_id)
    source = "probe." + args.role
    token = uuid.uuid4().hex
    sequence = 0
    replies = 0
    sent = {}
    started = time.monotonic()
    next_send = started
    try:
        while time.monotonic() - started < args.duration:
            for wire in bus.take_json("node_health", 32):
                sample = decode_model(wire, NodeHealth)
                if args.role == "echo" and sample.node_id == "probe.ping":
                    sequence += 1
                    publish_model(bus, "node_health", NodeHealth(
                        Header.now(source, sequence), source, NodeState.READY,
                        time.monotonic() - started, detail=sample.detail,
                    ))
                    replies += 1
                elif args.role == "ping" and sample.node_id == "probe.echo":
                    sent_at = sent.pop(sample.detail, None)
                    if sent_at is not None:
                        replies += 1
                        print(json.dumps({"reply": replies,
                                          "roundtrip_ms": round((time.monotonic()-sent_at)*1000, 2)}), flush=True)
            if args.role == "ping" and time.monotonic() >= next_send:
                sequence += 1
                detail = f"{token}:{sequence}"
                sent[detail] = time.monotonic()
                publish_model(bus, "node_health", NodeHealth(
                    Header.now(source, sequence), source, NodeState.READY,
                    time.monotonic() - started, detail=detail,
                ))
                next_send = time.monotonic() + 0.5
            if args.role == "ping" and replies >= 3:
                break
            time.sleep(0.01)
        print(json.dumps({"role": args.role, "replies": replies, "domain": args.domain_id}), flush=True)
        return 0 if replies >= 3 else 1
    finally:
        bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
