#!/usr/bin/env bash
# PCM capture must not start with a 1970 sample-clock origin before NTP steps.
set -euo pipefail
device="${1:?specify interface}"
address="${2:?specify static IPv4/prefix}"
echo "Waiting for $device $address and chrony correction <= 5ms"
for attempt in {1..180}; do
  if [[ "$(nmcli -g IP4.ADDRESS device show "$device" 2>/dev/null || true)" == "$address" ]] \
      && chronyc waitsync 1 0.005 >/dev/null 2>&1; then
    echo 'Wired address and synchronized clock are ready'
    exit 0
  fi
  sleep 1
done
echo 'Wired address/clock is not ready; service will retry' >&2
exit 1
