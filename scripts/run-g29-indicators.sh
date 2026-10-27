#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${CODRIVE_CARLA_PYTHON:-/home/khj/2026ws/carla/.venv/bin/python}" -m codrive.apps.g29_indicators "$@"
