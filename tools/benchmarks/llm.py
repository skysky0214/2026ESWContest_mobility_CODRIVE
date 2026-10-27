#!/usr/bin/env python3
"""Measure local RiskEvent-to-HMI text generation latency with llama.cpp."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path


DEFAULT_PROMPT = (
    "<|im_start|>system\n당신은 운전자 안전 안내 문장 생성기입니다. "
    "severity와 action을 변경하지 말고, 한국어 한 문장만 출력하세요. "
    "추론과 설명은 출력하지 마세요.<|im_end|>\n"
    "<|im_start|>user\n/no_think\n"
    'RiskEvent: {"code":"FRONT_PEDESTRIAN","severity":"EMERGENCY",'
    '"score":95,"recommended_action":"BRAKE","distance_m":4.2,"ttc_s":1.1}'
    "<|im_end|>\n<|im_start|>assistant\n"
)
_TOKENS_RE = re.compile(
    r"(?:speed:\s*(\d+(?:\.\d+)?)\s*t/s|(?:eval|generate).*?(\d+(?:\.\d+)?)\s+tokens/s)",
    re.I,
)


def run_once(args: argparse.Namespace) -> dict[str, object]:
    if Path(args.runner).name == "llama-simple":
        command = [args.runner, "-m", str(args.model), "-n", str(args.max_tokens), args.prompt]
    else:
        command = [
            args.runner,
            "-m",
            str(args.model),
            "-p",
            args.prompt,
            "-n",
            str(args.max_tokens),
            "--temp",
            "0",
            "--seed",
            "42",
            "--no-display-prompt",
            "--no-conversation",
        ]
    started = time.perf_counter_ns()
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    combined = f"{completed.stdout}\n{completed.stderr}"
    match = _TOKENS_RE.search(combined)
    return {
        "latency_ms": elapsed_ms,
        "tokens_per_second": float(next(value for value in match.groups() if value)) if match else None,
        "output": completed.stdout.strip(),
    }


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * fraction
    lower, upper = int(index), min(int(index) + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--runner", default="llama-simple")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=48)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.warmup < 0 or args.runs < 1 or args.max_tokens < 1:
        parser.error("warmup >= 0, runs >= 1, and max-tokens >= 1 are required")
    if not args.model.is_file():
        parser.error(f"model not found: {args.model}")

    for _ in range(args.warmup):
        run_once(args)
    samples = [run_once(args) for _ in range(args.runs)]
    latencies = [float(sample["latency_ms"]) for sample in samples]
    speeds = [float(sample["tokens_per_second"]) for sample in samples if sample["tokens_per_second"]]
    result = {
        "model": str(args.model),
        "runs": args.runs,
        "warmup": args.warmup,
        "max_tokens": args.max_tokens,
        "latency_ms": {
            "min": min(latencies),
            "mean": statistics.fmean(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies),
        },
        "tokens_per_second": statistics.fmean(speeds) if speeds else None,
        "sample_output": samples[-1]["output"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
