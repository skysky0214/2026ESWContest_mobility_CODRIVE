#!/usr/bin/env bash
set -eo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
carla_dir=${CODRIVE_CARLA_DIR:-$(dirname "$repo")/carla}
source "$carla_dir/ros2-env.sh"
# Board DDS configuration must not leak into the local ROS control participant.
unset CYCLONEDDS_URI
cd "$carla_dir"
exec "$carla_dir/.venv/bin/python" "$repo/tools/g29_ros_control.py"
