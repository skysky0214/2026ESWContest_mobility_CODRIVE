#!/usr/bin/env bash
# Run as root on the target board. Wi-Fi and its default route remain available.
set -euo pipefail
device="${1:?usage: configure-wired.sh DEVICE 192.168.50.HOST/24}"
address="${2:?specify the assigned board address}"
[[ "$EUID" == 0 ]] || { echo 'Run with sudo on the target board.' >&2; exit 1; }
[[ "$device" =~ ^[a-zA-Z0-9_.:-]+$ ]] && [[ -d "/sys/class/net/$device" ]]
[[ "$address" =~ ^192\.168\.50\.(10|21|22)/24$ ]]
if nmcli connection show codrive-lan >/dev/null 2>&1; then
  configured_device="$(nmcli -g connection.interface-name connection show codrive-lan)"
  [[ "$configured_device" == "$device" ]] || {
    echo 'Existing codrive-lan belongs to another interface; inspect it first.' >&2
    exit 1
  }
else
  nmcli connection add type ethernet con-name codrive-lan ifname "$device" \
    ipv4.method manual ipv4.addresses "$address"
fi
nmcli connection modify codrive-lan connection.autoconnect yes \
  connection.autoconnect-priority 100 ipv4.method manual ipv4.addresses "$address" \
  ipv4.gateway '' ipv4.dns '' ipv4.never-default yes ipv4.ignore-auto-dns yes \
  ipv4.dad-timeout 3000 ipv6.method link-local ipv6.never-default yes
nmcli connection up codrive-lan
# NetworkManager may report activation while IPv4 duplicate-address detection
# is still in progress and only the IPv6 link-local address is ready.
for attempt in {1..20}; do
  if [[ "$(nmcli -g IP4.ADDRESS device show "$device")" == "$address" ]]; then
    ip -br addr show dev "$device"
    exit 0
  fi
  sleep 0.5
done
echo "IPv4 address did not become ready on $device; inspect NetworkManager." >&2
exit 1
