"""Train and evaluate the compact siren/horn classifier."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .dataset import AudioTrainingDataset, records_by_partition
from .features import LogMelConfig
from .manifest import AudioManifestRecord, read_manifest
from .model import AudioEventNet, export_checkpoint_to_onnx, require_torch


def _limit(
    records: Sequence[AudioManifestRecord], maximum: int | None, seed: int
) -> list[AudioManifestRecord]:
    rows = list(records)
    if maximum is None or maximum >= len(rows):
        return rows
    generator = random.Random(seed)
    generator.shuffle(rows)
    return rows[:maximum]


def _metrics(targets: np.ndarray, probabilities: np.ndarray, thresholds: Sequence[float]) -> dict[str, Any]:
    predicted = probabilities >= np.asarray(thresholds, dtype=np.float32)
    truth = targets >= 0.5
    result: dict[str, Any] = {}
    for index, name in enumerate(("siren", "horn")):
        true_positive = int(np.logical_and(predicted[:, index], truth[:, index]).sum())
        false_positive = int(np.logical_and(predicted[:, index], ~truth[:, index]).sum())
        false_negative = int(np.logical_and(~predicted[:, index], truth[:, index]).sum())
        precision = true_positive / max(1, true_positive + false_positive)
        recall = true_positive / max(1, true_positive + false_negative)
        f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
        result[name] = {
            "threshold": float(thresholds[index]),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
        }
    return result


def _best_thresholds(targets: np.ndarray, probabilities: np.ndarray) -> tuple[float, float]:
    selected: list[float] = []
    for index in range(2):
        best = (float("-inf"), 0.72)
        for threshold in np.linspace(0.2, 0.95, 76):
            predicted = probabilities[:, index] >= threshold
            truth = targets[:, index] >= 0.5
            tp = int(np.logical_and(predicted, truth).sum())
            fp = int(np.logical_and(predicted, ~truth).sum())
            fn = int(np.logical_and(~predicted, truth).sum())
            precision = tp / max(1, tp + fp)
            recall = tp / max(1, tp + fn)
            f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
            if f1 > best[0]:
                best = (f1, float(threshold))
        selected.append(best[1])
    return selected[0], selected[1]


def _evaluate(model, loader, device) -> tuple[float, np.ndarray, np.ndarray]:
    framework, _ = require_torch()
    loss_function = framework.nn.BCEWithLogitsLoss()
    model.eval()
    losses: list[float] = []
    all_targets: list[np.ndarray] = []
    all_probabilities: list[np.ndarray] = []
    with framework.inference_mode():
        for features, targets in loader:
            features = features.to(device=device, dtype=framework.float32)
            targets = targets.to(device=device, dtype=framework.float32)
            logits = model(features)
            losses.append(float(loss_function(logits, targets).cpu()))
            all_targets.append(targets.cpu().numpy())
            all_probabilities.append(framework.sigmoid(logits).cpu().numpy())
    return (
        float(np.mean(losses)) if losses else 0.0,
        np.concatenate(all_targets),
        np.concatenate(all_probabilities),
    )


def train(args: argparse.Namespace) -> dict[str, Any]:
    framework, _ = require_torch()
    framework.manual_seed(args.seed)
    np.random.seed(args.seed)
    records = records_by_partition(read_manifest(args.manifest))
    train_rows = _limit(records.get("train", []), args.max_train_samples, args.seed)
    tune_rows = _limit(records.get("tune", []), args.max_eval_samples, args.seed + 1)
    test_rows = _limit(records.get("final_test", []), args.max_eval_samples, args.seed + 2)
    if not train_rows or not tune_rows or not test_rows:
        raise ValueError("manifest must contain train, tune, and final_test partitions")

    config = LogMelConfig()
    datasets = {
        "train": AudioTrainingDataset(train_rows, feature_config=config, augment=True, seed=args.seed),
        "tune": AudioTrainingDataset(tune_rows, feature_config=config, augment=False, seed=args.seed),
        "final_test": AudioTrainingDataset(test_rows, feature_config=config, augment=False, seed=args.seed),
    }
    loaders = {
        name: framework.utils.data.DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=name == "train",
            num_workers=args.workers,
            persistent_workers=args.workers > 0,
            pin_memory=args.device.startswith("cuda"),
        )
        for name, dataset in datasets.items()
    }

    device = framework.device(args.device)
    model = AudioEventNet().to(device)
    target_counts = np.asarray(
        [
            sum(row.siren_target for row in train_rows),
            sum(row.horn_target for row in train_rows),
        ],
        dtype=np.float32,
    )
    negative_counts = len(train_rows) - target_counts
    pos_weight = framework.tensor(
        np.minimum(negative_counts / np.maximum(target_counts, 1.0), 12.0),
        dtype=framework.float32,
        device=device,
    )
    loss_function = framework.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = framework.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    scheduler = framework.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, args.epochs))

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    best_path = output_dir / "audio_event_best.pt"
    best_tune_loss = float("inf")
    history: list[dict[str, Any]] = []
    started = time.monotonic()
    for epoch in range(1, args.epochs + 1):
        model.train()
        training_losses: list[float] = []
        for features, targets in loaders["train"]:
            features = features.to(device=device, dtype=framework.float32)
            targets = targets.to(device=device, dtype=framework.float32)
            optimizer.zero_grad(set_to_none=True)
            logits = model(features)
            loss = loss_function(logits, targets)
            loss.backward()
            framework.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            training_losses.append(float(loss.detach().cpu()))
        scheduler.step()
        tune_loss, tune_targets, tune_probabilities = _evaluate(model, loaders["tune"], device)
        thresholds = _best_thresholds(tune_targets, tune_probabilities)
        tune_metrics = _metrics(tune_targets, tune_probabilities, thresholds)
        epoch_result = {
            "epoch": epoch,
            "train_loss": float(np.mean(training_losses)),
            "tune_loss": tune_loss,
            "thresholds": thresholds,
            "metrics": tune_metrics,
        }
        history.append(epoch_result)
        print(json.dumps(epoch_result, ensure_ascii=False))
        if tune_loss < best_tune_loss:
            best_tune_loss = tune_loss
            framework.save(
                {
                    "model_state": model.state_dict(),
                    "model_version": "audio-event-v1",
                    "feature_config": asdict(config),
                    "thresholds": thresholds,
                    "target_names": ("siren", "horn"),
                    "epoch": epoch,
                    "tune_loss": tune_loss,
                },
                best_path,
            )

    try:
        checkpoint = framework.load(best_path, map_location=device, weights_only=False)
    except TypeError:
        checkpoint = framework.load(best_path, map_location=device)
    model.load_state_dict(checkpoint["model_state"])
    test_loss, test_targets, test_probabilities = _evaluate(model, loaders["final_test"], device)
    test_metrics = _metrics(test_targets, test_probabilities, checkpoint["thresholds"])
    report = {
        "checkpoint": str(best_path),
        "elapsed_s": time.monotonic() - started,
        "sample_counts": {name: len(dataset) for name, dataset in datasets.items()},
        "best_epoch": checkpoint["epoch"],
        "tune_loss": checkpoint["tune_loss"],
        "test_loss": test_loss,
        "thresholds": checkpoint["thresholds"],
        "final_test_metrics": test_metrics,
        "history": history,
    }
    if args.export_onnx:
        report["onnx"] = str(
            export_checkpoint_to_onnx(best_path, output_dir / "audio_event.onnx")
        )
    report_path = output_dir / "training_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/models/audio/model")
    )
    parser.add_argument("--epochs", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--device", default="cpu", help="cpu, cuda, or cuda:0")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--max-train-samples", type=int)
    parser.add_argument("--max-eval-samples", type=int)
    parser.add_argument("--export-onnx", action="store_true")
    args = parser.parse_args(argv)
    if args.epochs <= 0 or args.batch_size <= 0 or args.workers < 0:
        parser.error("epochs/batch-size must be positive and workers non-negative")
    report = train(args)
    print(json.dumps({key: value for key, value in report.items() if key != "history"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "train"]
