"""Artifact helpers shared by the experimental command-line tools."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import yaml

CLASSES = ["LOW", "MEDIUM", "HIGH"]
DISCLAIMER = "Synthetic-data research/demo model; not validated for real vehicle safety control."


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path):
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Configuration requires schema_version: 1")
    return value


def versions():
    result = {"python": platform.python_version(), "platform": platform.platform()}
    for name in ("numpy", "pandas", "scikit-learn", "xgboost", "shap", "PyYAML"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not installed"
    return result


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fresh_directory(path):
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"Output directory must be empty to avoid mixing runs: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path
