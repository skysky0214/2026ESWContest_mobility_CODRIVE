#!/usr/bin/env bash
# Select runtime profiles only; never changes source files or drives the vehicle.
set -euo pipefail
usage() {
  echo 'Usage: select-integration-mode.sh {carla|carla-screen|real-vehicle} [--dry-run] [--screen-calibrated]'
}
if [[ $# == 0 || ${1:-} == --help ]]; then usage; exit 0; fi
mode=$1; shift
dry=false; calibrated=false
for arg in "$@"; do
  case "$arg" in --dry-run) dry=true;; --screen-calibrated) calibrated=true;; *) usage >&2; exit 2;; esac
done
case "$mode" in carla|carla-screen|real-vehicle) ;; *) usage >&2; exit 2;; esac
if $calibrated && [[ $mode != carla-screen ]]; then echo '--screen-calibrated requires carla-screen' >&2; exit 2; fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
pi=${CODRIVE_PI_HOST:-rp@192.168.50.10}
front=${CODRIVE_FRONT_HOST:-one@192.168.50.21}
pi_root=${CODRIVE_PI_REPO:-/home/rp/ESW2026-can-v3}
front_root=${CODRIVE_FRONT_REPO:-/home/one/ws/ESW2026-main}
# These are remote shell paths, not arbitrary shell commands.
for path in "$pi_root" "$front_root"; do
  [[ $path =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Repository paths must be absolute without shell metacharacters' >&2; exit 2; }
done
case "$mode" in
  carla) pi_conf=pi-carla-demo.conf; front_conf=jetson1-carla-demo.conf;;
  carla-screen)
    pi_conf=pi-carla-screen.conf; front_conf=jetson1-carla-screen.conf
    if $calibrated; then pi_conf=pi-carla-screen-calibrated.conf; front_conf=jetson1-carla-screen-calibrated.conf; fi;;
  real-vehicle) pi_conf=pi-real-vehicle.conf; front_conf=jetson1-real-vehicle.conf;;
esac
for conf in "$pi_conf" "$front_conf"; do test -s "$repo/deploy/systemd-user/$conf"; done
printf 'Mode: %s\nPi profile: %s\nFront profile: %s\n' "$mode" "$pi_conf" "$front_conf"
if $calibrated; then echo 'Using explicit screen calibration: camera/monitor must match the current registration.'; fi
if $dry; then exit 0; fi
ssh_opts=(-o ConnectTimeout=8)
front_opts=("${ssh_opts[@]}")
if [[ -n ${CODRIVE_FRONT_SSH_CONTROL:-} ]]; then front_opts+=(-o "ControlPath=$CODRIVE_FRONT_SSH_CONTROL"); fi
# Local profiles are re-rendered for the configured remote checkout paths.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
python3 - "$repo/deploy/systemd-user" "$tmp" "$pi_conf" "$front_conf" "$pi_root" "$front_root" <<'PY'
from pathlib import Path
import sys
source,target,pi_conf,front_conf,pi_root,front_root=sys.argv[1:]
for name in (pi_conf,front_conf):
    data=(Path(source)/name).read_text().replace('/home/rp/ESW2026-can-v3',pi_root).replace('/home/one/ws/ESW2026-main',front_root)
    (Path(target)/name).write_text(data)
PY
# Preflight both hosts before mutating either profile. Runtime/model installation
# is documented separately; source deployment is intentionally not implicit here.
ssh "${ssh_opts[@]}" "$pi" "set -e; test -f '$pi_root/scripts/run-pi.sh'; test -f '$pi_root/config/risk.yaml'"
ssh "${front_opts[@]}" "$front" "set -e; test -f '$front_root/src/codrive/apps/jetson_front/main.py'"
scp "${ssh_opts[@]}" "$tmp/$pi_conf" "$pi:/tmp/codrive-next-pi.conf"
scp "${front_opts[@]}" "$tmp/$front_conf" "$front:/tmp/codrive-next-front.conf"
if [[ $mode == real-vehicle ]]; then ssh "${ssh_opts[@]}" "$pi" 'systemctl --user stop codrive-can-ecu'; fi
ssh "${front_opts[@]}" "$front" 'set -e; mkdir -p ~/.config/systemd/user/codrive-front-camera.service.d; install -m 644 /tmp/codrive-next-front.conf ~/.config/systemd/user/codrive-front-camera.service.d/99-zzzz-integration-mode.conf; systemctl --user daemon-reload; systemctl --user reset-failed codrive-front-camera; systemctl --user restart codrive-front-camera'
ssh "${ssh_opts[@]}" "$pi" 'set -e; mkdir -p ~/.config/systemd/user/codrive-pi.service.d; install -m 644 /tmp/codrive-next-pi.conf ~/.config/systemd/user/codrive-pi.service.d/99-zzzz-integration-mode.conf; systemctl --user daemon-reload; systemctl --user restart codrive-pi'
if [[ $mode != real-vehicle ]]; then ssh "${ssh_opts[@]}" "$pi" 'systemctl --user start codrive-can-ecu'; fi
printf 'Selected %s. Laptop CARLA programs and scenario files were preserved.\n' "$mode"
