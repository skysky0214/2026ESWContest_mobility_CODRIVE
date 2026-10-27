"""Generate synthetic final sensor sessions and fit a reproducible CPU model."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import platform
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import xgboost as xgb

from ..models import to_json
from ..v3_feature_engine import load_risk_policy
from ..sensor_features import RAW_FIELDS, SENSOR_FIELDS, VALIDITY_FIELDS
from .common import CLASSES, DISCLAIMER, fresh_directory, sha256, write_json
from .features_v3 import FEATURE_NAMES, normalize, probabilities, schema, usable, vector
from .synthetic import GENERATOR_VERSION, generate_sessions


def metrics(labels, probabilities):
    predicted = probabilities.argmax(axis=1)
    confusion = np.zeros((3, 3), dtype=int)
    for actual, guess in zip(labels, predicted):
        confusion[actual, guess] += 1
    per_class = {}
    for i, name in enumerate(CLASSES):
        tp = int(confusion[i, i])
        precision = tp / max(1, int(confusion[:, i].sum()))
        recall = tp / max(1, int(confusion[i].sum()))
        per_class[name] = {
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / max(1e-12, precision + recall),
            "support": int(confusion[i].sum()),
        }
    return {
        "rows": len(labels),
        "accuracy": float(np.mean(labels == predicted)),
        "macro_f1": float(np.mean([v["f1"] for v in per_class.values()])),
        "log_loss": float(
            -np.log(np.clip(probabilities[np.arange(len(labels)), labels], 1e-12, 1)).mean()
        ),
        "confusion_matrix": confusion.tolist(),
        "confusion_order": CLASSES,
        "per_class": per_class,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--rows-per-session", type=int, default=50)
    parser.add_argument("--risk-config", type=Path, default=Path("config/rules/final-sensors.yaml"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/models/risk/final-sensors-v3")
    )
    args = parser.parse_args(argv)
    policy = load_risk_policy(
        args.risk_config, cabin_observation_enabled=True, environment_ttl_ms=3000
    )
    output = fresh_directory(args.output)
    data = output / "data"
    data.mkdir()
    preprocessing = schema()
    records, vectors, labels = [], [], []
    print(f"Generating {args.samples} observations in independent sessions...", flush=True)
    with (
        (data / "dataset.jsonl").open("w", encoding="utf-8") as dataset,
        gzip.GzipFile(filename=str(data / "sensor_stream.jsonl.gz"), mode="wb", mtime=0) as stream,
    ):
        for item in generate_sessions(
            samples=args.samples,
            seed=args.seed,
            rows_per_session=args.rows_per_session,
            policy=policy,
        ):
            snapshot = item.pop("snapshot")
            row, _ = normalize(item["inputs"])
            if not usable(row):
                raise ValueError(
                    "All-missing observations must be unavailable, not training labels"
                )
            item["inputs"] = row
            dataset.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            stream.write((to_json(snapshot) + "\n").encode("utf-8"))
            records.append(item)
            vectors.append(vector(row, preprocessing))
            labels.append(CLASSES.index(item["target"]))
    if len(records) != args.samples:
        raise AssertionError("Incorrect generated dataset size")
    with (data / "dataset.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_id",
                "frame",
                "time_ns",
                "scenario",
                "split",
                "target",
                *RAW_FIELDS,
            ],
        )
        writer.writeheader()
        for item in records:
            writer.writerow(
                {
                    **{
                        k: item[k]
                        for k in ("session_id", "frame", "time_ns", "scenario", "split", "target")
                    },
                    **item["inputs"],
                }
            )
    x, y = np.stack(vectors), np.asarray(labels, dtype=np.int32)
    indices = {
        split: np.asarray([i for i, r in enumerate(records) if r["split"] == split])
        for split in ("train", "validation", "test")
    }
    groups = {split: {records[i]["session_id"] for i in ix} for split, ix in indices.items()}
    assert not (
        groups["train"] & groups["validation"]
        or groups["train"] & groups["test"]
        or groups["validation"] & groups["test"]
    )
    for split, ix in indices.items():
        if set(y[ix]) != {0, 1, 2}:
            raise ValueError(f"{split} must cover all three risk classes")
    parameters = {
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "tree_method": "hist",
        "device": "cpu",
        "nthread": 1,
        "seed": args.seed,
        "max_depth": 5,
        "eta": 0.05,
        "subsample": 0.85,
        "colsample_bytree": 0.9,
        "min_child_weight": 3,
        "lambda": 2,
    }
    matrices = {
        split: xgb.DMatrix(x[ix], label=y[ix], feature_names=list(FEATURE_NAMES), nthread=1)
        for split, ix in indices.items()
    }
    print("Training on train sessions; early stopping uses validation sessions only...", flush=True)
    history = {}
    started = time.perf_counter()
    model = xgb.train(
        parameters,
        matrices["train"],
        num_boost_round=400,
        evals=[(matrices["train"], "train"), (matrices["validation"], "validation")],
        evals_result=history,
        verbose_eval=50,
        callbacks=[xgb.callback.EarlyStopping(rounds=35, save_best=True)],
    )
    training_seconds = time.perf_counter() - started
    model.save_model(output / "model.ubj")
    write_json(output / "preprocess.json", preprocessing)
    gain = model.get_score(importance_type="gain")
    report = {
        "disclaimer": DISCLAIMER,
        "evaluation_scope": "Agreement with current RuleEngine labels on held-out synthetic sessions; NOT real-driving accuracy or accident probability",
        "samples": len(records),
        "sessions": len({r["session_id"] for r in records}),
        "seed": args.seed,
        "split_method": "session-disjoint 70/15/15; no row-level random split",
        "raw_features": len(RAW_FIELDS),
        "sensor_features": len(SENSOR_FIELDS),
        "validity_features": len(VALIDITY_FIELDS),
        "encoded_features": len(FEATURE_NAMES),
        "training_seconds": training_seconds,
        "boosting_rounds": model.num_boosted_rounds(),
        "best_iteration": model.best_iteration,
        "split_metrics": {
            split: metrics(y[ix], probabilities(model, x[ix])) for split, ix in indices.items()
        },
        "session_counts": {split: len(ids) for split, ids in groups.items()},
        "class_counts": {
            split: dict(Counter(records[i]["target"] for i in ix)) for split, ix in indices.items()
        },
        "scenario_counts": dict(Counter(r["scenario"] for r in records)),
        "null_counts": {
            name: sum(r["inputs"][name] is None for r in records) for name in SENSOR_FIELDS
        },
        "validity_counts": {
            name: dict(Counter(str(r["inputs"][name]) for r in records)) for name in VALIDITY_FIELDS
        },
        "feature_gain": {name: float(gain.get(name, 0)) for name in FEATURE_NAMES},
        "unused_encoded_features": [name for name in FEATURE_NAMES if name not in gain],
        "direction_note": "The teacher assigns equal risk to left/right. Inference averages mirrored directional predictions to enforce equal risk; warning templates retain the actual direction. Raw tree gain is not causal attribution.",
        "limits": [
            "Synthetic rule imitation; not independent safety evidence",
            "Only generated scenarios and failure patterns were evaluated",
            "CRITICAL and WARNING both map to HIGH; rule CRITICAL priority remains",
            "No lane, TTC/ranging, steering, rapid-launch or unintended-acceleration diagnosis",
            "Real audio direction estimator and CARLA/CAN adapter are separate producers",
        ],
    }
    # End-to-end latency includes normalization and deterministic warning templates.
    metadata = {
        "experiment": "final-sensors-v3",
        "feature_contract_version": 3,
        "classes": CLASSES,
        "disclaimer": DISCLAIMER,
        "model_sha256": sha256(output / "model.ubj"),
        "model_bytes": (output / "model.ubj").stat().st_size,
        "parameters": parameters,
        "rule_policy": asdict(policy),
        "generator_version": GENERATOR_VERSION,
        "generator_seed": args.seed,
        "training_samples": args.samples,
        "label_source": "RuleEngine.last_candidates BEFORE event cooldown; max severity, no rule score input",
        "versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "xgboost": xgb.__version__,
        },
        "training_seconds": training_seconds,
        "dataset_sha256": sha256(data / "dataset.jsonl"),
        "source_sha256": {
            str(p.name): sha256(p)
            for p in (
                Path(__file__),
                Path(__file__).with_name("synthetic.py"),
                Path(__file__).with_name("features_v3.py"),
                Path(__file__).parents[1] / "risk.py",
                Path(__file__).parents[1] / "sensor_features.py",
                args.risk_config,
            )
        },
        "mode": "display_only",
        "trained_from_scratch": True,
        "direction_policy": "average_left_right_predictions",
    }
    write_json(output / "metadata.json", metadata)
    from .infer import Predictor

    predictor = Predictor(output)
    test_rows = [records[i]["inputs"] for i in indices["test"]]
    for row in test_rows[:25]:
        predictor.predict(row)
    times = []
    for i in range(1000):
        start = time.perf_counter_ns()
        predictor.predict(test_rows[i % len(test_rows)])
        times.append((time.perf_counter_ns() - start) / 1e6)
    report["inference_benchmark"] = {
        "host": platform.platform(),
        "architecture": platform.machine(),
        "iterations": len(times),
        "p50_ms": float(np.percentile(times, 50)),
        "p95_ms": float(np.percentile(times, 95)),
        "max_ms": max(times),
        "scope": "Host-only CPU predictor, one thread; not a Raspberry Pi measurement",
    }
    write_json(output / "training_report.json", report)
    write_json(output / "learning_curve.json", history)
    write_json(output / "sample_input.json", test_rows[0])
    test_probs = probabilities(model, x[indices["test"]])
    with (data / "test_predictions.jsonl").open("w", encoding="utf-8") as handle:
        for i, probs in zip(indices["test"], test_probs):
            handle.write(
                json.dumps(
                    {
                        "session_id": records[i]["session_id"],
                        "frame": records[i]["frame"],
                        "target": records[i]["target"],
                        "prediction": CLASSES[int(probs.argmax())],
                        "probabilities": dict(zip(CLASSES, map(float, probs))),
                    }
                )
                + "\n"
            )
    test = report["split_metrics"]["test"]
    (output / "README.md").write_text(
        "# Final sensor XGBoost v3\n\n"
        f"합성 관측 {len(records):,}개, {report['sessions']}개 세션. "
        "28개 관측·파생값 + 8개 유효성 표시를 52개 모델 컬럼으로 인코딩한다.\n\n"
        "정답은 현재 RuleEngine의 후보 중 최대 severity다. 반복 경고 cooldown은 정답에 "
        "영향을 주지 않는다. 규칙 점수·정답·세션 ID·시나리오·시각은 모델 입력에서 제외한다. "
        "후보 없음=LOW, INFO=MEDIUM, WARNING/CRITICAL=HIGH.\n\n"
        "세션 단위로 70/15/15 분리했고 validation만 early stopping에 사용했다. "
        f"최종 합성 test 일치율 {test['accuracy']:.2%}, macro F1 {test['macro_f1']:.4f}. "
        "실제 주행 정확도나 사고 확률을 뜻하지 않는다. 실차/CARLA 데이터 검증은 별도다.\n\n"
        "음향 left/right는 대칭 예측을 평균해 같은 위험도로 처리한다. 안내 방향은 실제 입력을 "
        "유지한다. NaN은 결측이며 정상/0으로 대체하지 않는다. 전부 미수신이면 판단하지 않는다.\n\n"
        "- `model.ubj`, `preprocess.json`, `metadata.json`: 함께 배포할 CPU 추론 파일\n"
        "- `training_report.json`, `learning_curve.json`: 평가·파라미터·중요도·호스트 벤치마크\n"
        "- `data/dataset.csv`, `data/dataset.jsonl`: 정확히 10,000개의 특징·정답·세션 분할\n"
        "- `data/sensor_stream.jsonl.gz`: 특징을 재계산할 원본 가상 센서 메시지\n"
        "- `data/test_predictions.jsonl`: 최종 test의 정답·확률·예측\n"
        "- `manifest.json`: 산출물 SHA256\n\n"
        "저장소 루트에서, 별도의 빈 output 디렉터리를 지정해 재현한다.\n\n"
        "```bash\n"
        "PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.train "
        f"--samples {args.samples} --seed {args.seed} --output artifacts/models/risk/reproduced-v3\n"
        "PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.infer "
        f"--model-dir {output} --input {output}/sample_input.json\n"
        "```\n\n"
        "현재 Pi 경로는 화면 표시 전용이다. `--xgboost-model`로 이 디렉터리를 지정하면 "
        "기존 Rule-based risk와 함께 실행한다. 실제 보드 배포는 이 학습 작업에 포함되지 않는다.\n",
        encoding="utf-8",
    )
    write_json(
        output / "manifest.json",
        {
            str(path.relative_to(output)): sha256(path)
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        },
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "test": report["split_metrics"]["test"],
                "benchmark": report["inference_benchmark"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
