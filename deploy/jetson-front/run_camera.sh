#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/activate.sh"
cd -- "$CODRIVE_REPO_ROOT"
exec python -m codrive.apps.jetson_front --backend v4l2 --device-index 0 --width 3840 --height 2160 --fps 30 --pixel-format UYVY --lane-method colour --lane-interval 1 "$@"
