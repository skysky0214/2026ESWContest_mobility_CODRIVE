#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${CODRIVE_LOG_TO_FILE:-0}" == "1" ]]; then
  mkdir -p logs
  exec >> logs/pi.log 2>&1
fi
export PYTHONPATH="$PWD/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
export CYCLONEDDS_HOME="$HOME/.local/opt/cyclonedds"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export CODRIVE_CABIN_CAMERA_STREAM="${CODRIVE_CABIN_CAMERA_STREAM-http://192.168.50.22:8082/stream.mjpg}"
exec "${CODRIVE_PYTHON:-python3}" -m codrive.apps.pi --dds-config config/integration/cyclonedds-pi-wired.xml --tts "$@"
