#!/usr/bin/env bash
set -eo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
carla_dir=${CODRIVE_CARLA_DIR:-$(dirname "$repo")/carla}
source "$carla_dir/ros2-env.sh"
# DDS Python binding is installed in the laptop user site.
unset PYTHONNOUSERSITE
export PYTHONPATH="$repo/board_runtime/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$repo"
# Keep steering/pedals alive when a scenario is stopped or replaced.
if [[ " $* " != *" --no-g29 "* ]]; then
  if ! systemctl --user is-active --quiet codrive-carla-g29; then
    systemd-run --user --unit=codrive-carla-g29 --collect \
      -p Restart=on-failure -p RestartSec=2 \
      -p "StandardOutput=append:/tmp/codrive-carla-g29.log" \
      -p "StandardError=append:/tmp/codrive-carla-g29.log" \
      /bin/bash "$repo/scripts/run-carla-g29.sh"
  fi
  set -- "$@" --no-g29
fi
exec "$carla_dir/.venv/bin/python" "$repo/tools/carla_saved_scenario.py" --carla-dir "$carla_dir" "$@"
