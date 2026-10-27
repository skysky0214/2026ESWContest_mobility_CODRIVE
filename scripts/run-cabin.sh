#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${CODRIVE_LOG_TO_FILE:-0}" == "1" ]]; then
  mkdir -p logs
  exec >> logs/dms.log 2>&1
fi
export PYTHONPATH="$PWD/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
export CYCLONEDDS_HOME="$HOME/.local/opt/cyclonedds"
export CYCLONEDDS_URI="file://$PWD/config/integration/cyclonedds-jetson2-wired.xml"
exec python3 -m codrive.apps.dms --config config/integration/jetson-cabin.yaml "$@"
