"""Evaluate a trained audio checkpoint on an untouched manifest partition."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from .dataset import AudioTrainingDataset
from .manifest import AudioManifestRecord, read_manifest
from .model import TorchCheckpointBackend, require_torch
from .train import _evaluate, _limit, _metrics


def _error_slices(
    records: list[AudioManifestRecord],
    targets: np.ndarray,
    probabilities: np.ndarray,
    thresholds: tuple[float, float],
) -> dict[str, Any]:
    predicted = probabilities >= np.asarray(thresholds, dtype=np.float32)
    truth = targets >= 0.5
    slices: dict[str, Any] = {}
    for index, name in enumerate(("siren", "horn")):
        false_positive = np.logical_and(predicted[:, index], ~truth[:, index])
        false_negative = np.logical_and(~predicted[:, index], truth[:, index])
        negative_count = int((~truth[:, index]).sum())
        slices[name] = {
            "false_positive_by_category_03": dict(
                sorted(
                    Counter(
                        row.category_03 for row, selected in zip(records, false_positive) if selected
                    ).items()
                )
            ),
            "false_negative_by_category_03": dict(
                sorted(
                    Counter(
                        row.category_03 for row, selected in zip(records, false_negative) if selected
                    ).items()
                )
            ),
            "false_positives_per_1000_negative_clips": (
                float(false_positive.sum()) * 1_000.0 / max(1, negative_count)
            ),
        }
    return slices


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    framework, _ = require_torch()
    rows = [row for row in read_manifest(args.manifest) if row.partition == args.partition]
    rows = _limit(rows, args.max_samples, args.seed)
    if not rows:
        raise ValueError(f"manifest has no records in partition {args.partition!r}")

    backend = TorchCheckpointBackend(args.checkpoint, device=args.device)
    dataset = AudioTrainingDataset(
        rows,
        feature_config=backend.feature_config,
        augment=False,
        seed=args.seed,
    )
    loader = framework.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=args.workers > 0,
        pin_memory=args.device.startswith("cuda"),
    )
    started = time.monotonic()
    loss, targets, probabilities = _evaluate(backend.model, loader, backend.device)
    elapsed = time.monotonic() - started
    thresholds = tuple(float(value) for value in backend.thresholds)
    result = {
        "checkpoint": str(backend.path),
        "model_version": backend.model_version,
        "partition": args.partition,
        "sample_count": len(rows),
        "loss": loss,
        "thresholds": thresholds,
        "metrics": _metrics(targets, probabilities, thresholds),
        "error_slices": _error_slices(rows, targets, probabilities, thresholds),
        "elapsed_s": elapsed,
        "clips_per_second": len(rows) / max(elapsed, 1e-9),
    }
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument(
        "--partition", choices=("train", "tune", "final_test"), default="final_test"
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--max-samples", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.batch_size <= 0 or args.workers < 0:
        parser.error("batch-size must be positive and workers non-negative")
    print(json.dumps(evaluate(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["evaluate", "main"]
