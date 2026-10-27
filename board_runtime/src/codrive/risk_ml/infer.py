"""CPU inference for legacy B/v2 and the final sensor v3 models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from xgboost import Booster

from . import features as legacy_features
from . import features_v3
from .common import CLASSES, read_json, sha256
from .context import DEFAULT_CONTEXT_POLICY
from .messages import warning


class Predictor:
    def __init__(self, model_dir: str | Path) -> None:
        path = Path(model_dir)
        self.metadata = read_json(path / "metadata.json")
        self.preprocessing = read_json(path / "preprocess.json")
        self.contract_version = self.metadata.get("feature_contract_version")
        self.features = features_v3 if self.contract_version == 3 else legacy_features
        self.features.validate_schema(self.preprocessing)
        if (
            (self.contract_version, self.metadata.get("experiment"))
            not in {(2, "B"), (3, "final-sensors-v3")}
            or self.preprocessing["hybrid"]
            or self.metadata.get("classes") != CLASSES
        ):
            raise ValueError("Pi integration requires B/v2 or final-sensors-v3")
        if (
            self.contract_version == 3
            and self.metadata.get("direction_policy") != "average_left_right_predictions"
        ):
            raise ValueError("Final sensor model requires the symmetric audio direction policy")
        if sha256(path / "model.ubj") != self.metadata["model_sha256"]:
            raise ValueError("Model checksum mismatch")
        self.context_policy = self.metadata.get("context_policy", DEFAULT_CONTEXT_POLICY)
        self.model = Booster(params={"nthread": 1, "device": "cpu"})
        self.model.load_model(path / "model.ubj")
        self.model.set_param({"nthread": 1, "device": "cpu"})
        if self.model.num_features() != len(self.preprocessing["feature_names"]):
            raise ValueError("Model and preprocessing contract mismatch")
        if (
            self.model.feature_names
            and self.model.feature_names != self.preprocessing["feature_names"]
        ):
            raise ValueError("Model feature order mismatch")

    def predict(self, sample: dict) -> dict:
        row, missing = self.features.normalize(sample)
        usable = (
            features_v3.usable(row)
            if self.contract_version == 3
            else any(value is not None for value in row.values())
        )
        if not usable:
            raise ValueError("No usable sensor observations")
        values = self.features.vector(row, self.preprocessing)[None, :]
        probs = np.asarray(
            (
                features_v3.probabilities(self.model, values)
                if self.contract_version == 3
                else self.model.inplace_predict(values)
            )[0]
        )
        if (
            probs.shape != (len(CLASSES),)
            or not np.isfinite(probs).all()
            or (probs < 0).any()
            or (probs > 1).any()
            or not np.isclose(probs.sum(), 1.0, atol=1e-5)
        ):
            raise ValueError("Model returned invalid class probabilities")
        level = CLASSES[int(np.argmax(probs))]
        if self.contract_version == 3:
            from .messages import warning_v3

            factors, message = warning_v3(row, level)
        else:
            factors, message = warning(row, level, self.context_policy)
        if missing:
            message += " 일부 센서 정보가 누락되어 판단이 제한됩니다."
        return {
            "status": "ok",
            "xgboost_risk_level": level,
            "risk_probability": dict(zip(CLASSES, map(float, probs))),
            "warning_message": message,
            "main_risk_factors": factors,
            "explanation_source": "observed_condition_templates",
            "degraded": bool(missing),
            "missing_fields": missing,
            "model_version": self.metadata["model_sha256"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    predictor = Predictor(args.model_dir)

    def reject_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}; use null")

    sample = json.loads(Path(args.input).read_text(), parse_constant=reject_constant)
    print(json.dumps(predictor.predict(sample), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
