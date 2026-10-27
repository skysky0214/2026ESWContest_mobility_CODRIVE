"""Activate a reviewed main checkout on one board using reversible user drop-ins.

Keeps installed JetPack/Pi Python environments and old execution folders intact.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("front", "cabin", "pi"), required=True)
    parser.add_argument("--python", help="Existing Pi risk venv or Jetson front Python")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if run("git", "-C", str(repo), "branch", "--show-current") != "main":
        parser.error("Activate a main checkout, not a development branch")
    commit = run("git", "-C", str(repo), "rev-parse", "HEAD")
    home = Path.home()
    backup_root = home / "codrive-backups"
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="main-" + args.role + ".", dir=backup_root))
    units = {
        "front": {"codrive-front-standby": "run-front-standby.sh", "codrive-front-camera": None},
        "cabin": {"codrive-cabin-link": "run-cabin-link.sh", "codrive-audio": "run-audio.sh", "codrive-dms": "run-cabin.sh"},
        "pi": {"codrive-pi": "run-pi.sh"},
    }[args.role]
    rollback = ["#!/bin/bash", "set -eu"]
    for unit, script in units.items():
        folder = home / ".config/systemd/user" / (unit + ".service.d")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "60-main.conf"
        if target.exists():
            original = backup / (unit + ".conf")
            shutil.copy2(target, original)
            rollback.append("cp " + shlex.quote(str(original)) + " " + shlex.quote(str(target)))
        else:
            rollback.append("python3 -c " + shlex.quote("from pathlib import Path; Path(" + repr(str(target)) + ").unlink(missing_ok=True)"))
        lines = ["[Service]", f"WorkingDirectory={repo}"]
        if script:
            lines += ["ExecStart=", f"ExecStart=/bin/bash {repo}/scripts/{script}"]
            if args.role == "pi":
                if not args.python:
                    parser.error("Pi activation requires --python pointing to its existing risk venv")
                lines[-1] += " --xgboost-model artifacts/models/risk/B"
                lines.append("Environment=CODRIVE_PYTHON=" + args.python)
        else:
            if not args.python:
                parser.error("Front activation requires --python pointing to its existing JetPack venv")
            engine = repo / "artifacts/models/vision/yolo11n.engine"
            source = home / "ws/adas/yolo11n.engine"
            if not source.is_file():
                parser.error("Existing board-specific TensorRT engine is missing")
            engine.parent.mkdir(parents=True, exist_ok=True)
            if not engine.exists():
                engine.symlink_to(source)
            lines += [f"Environment=PYTHONPATH={repo}/src", "ExecStart=",
                      f"ExecStart={args.python} -m codrive.apps.jetson_front --backend v4l2 --device-index 0 --width 3840 --height 2160 --fps 30 --pixel-format UYVY --lane-method colour --lane-interval 1 --headless --dry-run"]
        if args.role == "cabin" and unit == "codrive-audio":
            lines[-1] += " --diagnostics"
        if script:
            interface = "eth0" if args.role == "pi" else "enP8p1s0"
            address = {"front": "192.168.50.21/24", "cabin": "192.168.50.22/24", "pi": "192.168.50.10/24"}[args.role]
            lines += ["ExecStartPre=", f"ExecStartPre=/bin/bash {repo}/scripts/wait-wired-clock.sh {interface} {address}"]
        target.write_text("\n".join(lines) + "\n")
    rollback += ["systemctl --user daemon-reload", "systemctl --user restart " + " ".join(units)]
    rollback_path = backup / "rollback.sh"
    rollback_path.write_text("\n".join(rollback) + "\n")
    rollback_path.chmod(0o700)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", *units], check=True)
    result = {"branch": "main", "commit": commit, "role": args.role, "repo": str(repo),
              "rollback": str(rollback_path)}
    (repo / ".codrive-deployment.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
