"""Small, strict-enough YAML configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ConfigError(f"{config_path}: root must be a mapping")
    if value.get("schema_version") != 1:
        raise ConfigError(f"{config_path}: unsupported or missing schema_version")
    for key in ("node_id", "role", "dds"):
        if key not in value:
            raise ConfigError(f"{config_path}: missing required key {key!r}")
    dds = value["dds"]
    if not isinstance(dds, dict) or not isinstance(dds.get("domain_id"), int):
        raise ConfigError(f"{config_path}: dds.domain_id must be an integer")
    return value
