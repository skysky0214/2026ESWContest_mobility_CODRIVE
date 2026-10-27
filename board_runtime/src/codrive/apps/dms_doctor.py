"""Print local Jetson DMS readiness without installing or configuring anything."""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import platform
from pathlib import Path


def inspect_environment(model: str | None = None) -> dict:
    result = {
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cameras": glob.glob("/dev/video*"),
        "modules": {},
    }
    for label, path in (("board", "/proc/device-tree/model"), ("l4t", "/etc/nv_tegra_release")):
        if Path(path).is_file():
            result[label] = Path(path).read_text().strip("\x00\n")
    for name in ("cv2", "numpy", "tensorrt", "torch", "ultralytics", "cyclonedds"):
        try:
            module = importlib.import_module(name)
            entry = {"available": True, "version": str(getattr(module, "__version__", "unknown"))}
            if name == "cv2":
                entry["gstreamer"] = any(
                    "GStreamer:" in line and "YES" in line
                    for line in module.getBuildInformation().splitlines()
                )
            if name == "torch":
                entry["cuda_available"] = module.cuda.is_available()
                entry["cuda_version"] = module.version.cuda
            result["modules"][name] = entry
        except (ImportError, OSError, RuntimeError) as exc:
            result["modules"][name] = {"available": False, "error": str(exc)}
    if model:
        result["model"] = {"path": model, "exists": Path(model).is_file()}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    print(json.dumps(inspect_environment(args.model), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
