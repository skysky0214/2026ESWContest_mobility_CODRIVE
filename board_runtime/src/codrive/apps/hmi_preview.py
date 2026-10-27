"""Local synthetic HMI preview. Creates no DDS participant and never invokes TTS."""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from pathlib import Path

from ..models import DriverState, Header, SituationSnapshot, to_json
from ..risk_ml.shadow import ShadowRisk
from .pi import DashboardState, make_http_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path("artifacts/models/risk/B"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--scenario", choices=("normal", "phone", "drowsy", "missing"), default="phone"
    )
    parser.add_argument("--duration", type=float)
    args = parser.parse_args(argv)
    from ..risk_ml.infer import Predictor

    shadow = ShadowRisk(Predictor(args.model_dir))
    state = DashboardState()
    server = make_http_server(args.host, args.port, state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(
        f"Local synthetic preview: http://{args.host}:{server.server_port} (no DDS / TTS)",
        flush=True,
    )
    started = time.monotonic()
    sequence = 0
    try:
        while not stop.is_set():
            now = time.time_ns()
            sequence += 1
            driver = (
                None
                if args.scenario == "missing"
                else DriverState(
                    Header.now("preview.dms", sequence, capture_time_ns=now),
                    "cabin",
                    "fixture",
                    attention_score=0.2 if args.scenario == "phone" else 0.9,
                    looking_forward=args.scenario != "phone",
                    phone_detected=args.scenario == "phone",
                    drowsiness_score=0.95 if args.scenario == "drowsy" else 0.0,
                )
            )
            snapshot = SituationSnapshot(
                Header.now("preview.fusion", sequence, capture_time_ns=now), driver=driver
            )
            state.update(
                {
                    "preview": True,
                    "status": "preview",
                    "updated_time_ns": now,
                    "snapshot": json.loads(to_json(snapshot)),
                    "risk_model": shadow.update(snapshot, now_ns=now),
                    "received_samples": {"driver_state": sequence if driver else 0},
                    "speech": {"enabled": False},
                    "command": None,
                    "events": [],
                }
            )
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            stop.wait(0.05)
    finally:
        server.shutdown()
        server.server_close()
        shadow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
