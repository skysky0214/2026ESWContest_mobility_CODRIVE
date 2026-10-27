#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
if [[ "${CODRIVE_LOG_TO_FILE:-0}" == "1" ]]; then
  exec >> logs/can-ecu.log 2>&1
fi
export PYTHONPATH="$PWD/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
export CYCLONEDDS_HOME="$HOME/.local/opt/cyclonedds"
exec "${CODRIVE_PYTHON:-python3}" -m codrive.apps.can_ecu "$@"
