from pathlib import Path

from codrive.config import load_config


ROOT = Path(__file__).resolve().parents[2]


def test_all_device_configs_load() -> None:
    configs = [load_config(path) for path in sorted((ROOT / "config/board").glob("*.yaml"))]
    assert {item["node_id"] for item in configs} == {"jetson-front", "jetson-cabin", "pi5"}
    assert {item["dds"]["domain_id"] for item in configs} == {42}


def test_pi_panda_is_receive_only() -> None:
    config = load_config(ROOT / "config/board/pi5.yaml")
    assert config["panda"]["receive_only"] is True
