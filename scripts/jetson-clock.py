#!/usr/bin/python3
"""Restore a persisted clock floor after Jetson RTC probing, then let chrony sync."""
import argparse
import csv
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("/var/lib/codrive-clock/last-sync")
MIN_TIME = 1767225600  # 2026-01-01 UTC, project deployment lower bound
MAX_TIME = 4102444800  # 2100-01-01 UTC


def valid_time(value: float) -> bool:
    return math.isfinite(value) and MIN_TIME <= value < MAX_TIME


def read_checkpoint(path: Path) -> float | None:
    try:
        value = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return value if valid_time(value) else None


def synced_time(report: str, now: float) -> bool:
    """Require a recent measurement from the wired Pi, not merely a saved date."""
    try:
        fields = next(csv.reader([report.strip()]))
        return (
            len(fields) == 14
            and fields[1] == "192.168.50.10"
            and 0 < int(fields[2]) < 16
            and -1 <= now - float(fields[3]) <= 120
            and abs(float(fields[4])) <= 0.005
            and fields[13] == "Normal"
            and valid_time(now)
        )
    except (ValueError, IndexError, StopIteration):
        return False


def write_checkpoint(path: Path, now: float) -> None:
    """Replace atomically and flush both file and directory for power loss."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".last-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(f"{now:.6f}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore(path: Path, wait_rtc: bool) -> None:
    # The PMIC driver sets CLOCK_REALTIME while registering rtc0, *after* PID 1
    # starts. Restoring earlier would be overwritten by its 1970 reading.
    if wait_rtc:
        deadline = time.monotonic() + 30
        while not Path("/dev/rtc0").exists():
            if time.monotonic() >= deadline:
                print("RTC0 did not appear in 30s; restoring saved clock anyway", flush=True)
                break
            time.sleep(0.1)
    saved = read_checkpoint(path)
    if saved is None:
        print("No valid saved clock; waiting for chrony", flush=True)
    elif not valid_time(time.time()) or time.time() < saved:
        time.clock_settime(time.CLOCK_REALTIME, saved)
        print(f"Restored clock floor: {datetime.fromtimestamp(saved, timezone.utc)}", flush=True)
    else:
        print("System/RTC clock is newer than saved clock; kept current time", flush=True)


def save(path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/chronyc", "-c", "tracking"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    now = time.time()
    if result.returncode or not synced_time(result.stdout, now):
        print("No recent synchronized Pi time; kept previous checkpoint", flush=True)
        return
    # A fresh NTP measurement may correct an erroneous future checkpoint too.
    write_checkpoint(path, now)
    print(f"Saved synchronized clock: {datetime.fromtimestamp(now, timezone.utc)}", flush=True)
    result = subprocess.run(
        ["/usr/sbin/hwclock", "--systohc", "--utc", "--noadjfile", "--rtc=/dev/rtc0"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode:
        print(f"RTC0 write failed; disk checkpoint retained: {result.stderr.strip()}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "save"))
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--wait-rtc", action="store_true")
    args = parser.parse_args()
    if args.action == "restore":
        restore(args.state, args.wait_rtc)
    else:
        save(args.state)


if __name__ == "__main__":
    main()
