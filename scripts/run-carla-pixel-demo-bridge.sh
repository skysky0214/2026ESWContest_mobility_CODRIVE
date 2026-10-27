#!/usr/bin/env bash
set -eo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
carla_dir=${CODRIVE_CARLA_DIR:-$(dirname "$repo")/carla}
cd "$carla_dir"
source ./ros2-env.sh
exec .venv/bin/python -m carla_ros_bridge.bridge --ros-args -p host:=127.0.0.1 -p port:=2000 -p timeout:=30 -p passive:=true -p synchronous_mode:=false -p "ego_vehicle_role_name:=['hero']" -p register_all_sensors:=false
