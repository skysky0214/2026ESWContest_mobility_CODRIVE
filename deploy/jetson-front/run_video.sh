#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/activate.sh"
cd -- "$CODRIVE_REPO_ROOT"
exec python -m codrive.apps.jetson_front --video artifacts/recordings/test_driving.mp4 --loop --lane-method colour --lane-interval 1 "$@"
