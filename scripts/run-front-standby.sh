#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${CODRIVE_LOG_TO_FILE:-0}" == "1" ]]; then
  mkdir -p logs
  exec >> logs/front-standby.log 2>&1
fi
export PYTHONPATH="$PWD/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
export CYCLONEDDS_HOME="$HOME/.local/opt/cyclonedds"
exec python3 -m codrive.apps.standby --config config/integration/jetson-front.yaml "$@"
