#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $EUID -ne 0 ]]; then
  echo 'Run this installer with sudo on the target Jetson.' >&2
  exit 1
fi
if [[ $(cat /sys/class/rtc/rtc0/hctosys) != 1 ]]; then
  echo 'RTC0 is not the boot clock; inspect RTC ordering before installing.' >&2
  exit 1
fi
# Seed from trusted NTP before enabling the fallback. Never seed with 1970.
chronyc waitsync 1 0.005
backup="/var/backups/codrive-clock-$(date +%Y%m%dT%H%M%S)"
install -d -m 700 "$backup"
for file in /usr/local/libexec/codrive-clock.py /var/lib/codrive-clock/last-sync \
  /etc/systemd/system/codrive-clock-{restore.service,save.service,save.timer}; do
  if [[ -f $file ]]; then cp --parents "$file" "$backup/"; fi
done
install -d /usr/local/libexec /var/lib/codrive-clock
install -m 755 "$root/scripts/jetson-clock.py" /usr/local/libexec/codrive-clock.py
install -m 644 "$root"/deploy/systemd/system/codrive-clock-* /etc/systemd/system/
/usr/bin/python3 /usr/local/libexec/codrive-clock.py save
test -s /var/lib/codrive-clock/last-sync
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/codrive-clock-*.service \
  /etc/systemd/system/codrive-clock-save.timer
systemctl enable --now chrony codrive-clock-restore.service codrive-clock-save.timer
echo "Clock fallback installed. Previous files: $backup"
