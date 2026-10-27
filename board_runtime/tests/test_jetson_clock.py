"""Boot clock fallback must not turn an unsynchronized date into trusted time."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "jetson_clock", Path(__file__).parents[2] / "scripts/jetson-clock.py"
)
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)
NOW = 1792580400.0
REPORT = f"C0A8320A,192.168.50.10,5,{NOW - 2},0.000001,0,0,0,0,0,0,0,4,Normal"


@pytest.mark.parametrize("text", ["1970", "nan", "inf", "broken", "99999999999"])
def test_bad_checkpoint_never_sets_system_clock(tmp_path, monkeypatch, text):
    path = tmp_path / "last-sync"
    path.write_text(text)
    monkeypatch.setattr(clock.time, "clock_settime", lambda *_: pytest.fail("clock changed"))
    clock.restore(path, False)


@pytest.mark.parametrize("rtc_time", [30, 4154472317])
def test_restore_rejects_epoch_or_overflow_but_preserves_newer_valid_rtc(
    tmp_path, monkeypatch, rtc_time
):
    path = tmp_path / "last-sync"
    clock.write_checkpoint(path, NOW)
    changes = []
    monkeypatch.setattr(clock.time, "clock_settime", lambda _, value: changes.append(value))
    monkeypatch.setattr(clock.time, "time", lambda: rtc_time)
    clock.restore(path, False)
    assert changes == [NOW]
    changes.clear()
    monkeypatch.setattr(clock.time, "time", lambda: NOW + 100)
    clock.restore(path, False)
    assert not changes


@pytest.mark.parametrize("report", [
    REPORT.replace("Normal", "Not synchronised"),
    REPORT.replace("192.168.50.10", "127.127.1.1"),
    REPORT.replace(str(NOW - 2), str(NOW - 300)),
    REPORT.replace("0.000001", "0.5"),
    REPORT.replace(str(NOW - 2), "nan"),
    "invalid",
])
def test_untrusted_ntp_report_cannot_replace_checkpoint(report):
    assert not clock.synced_time(report, NOW)


def test_recent_wired_ntp_is_trusted():
    assert clock.synced_time(REPORT, NOW)
    assert not clock.synced_time(REPORT, 30)


def test_unsynchronized_save_preserves_checkpoint_and_does_not_write_rtc(tmp_path, monkeypatch):
    path = tmp_path / "last-sync"
    clock.write_checkpoint(path, NOW - 60)
    calls = []

    def run(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=REPORT.replace("Normal", "Not synchronised"))

    monkeypatch.setattr(clock.subprocess, "run", run)
    monkeypatch.setattr(clock.time, "time", lambda: NOW)
    clock.save(path)
    assert clock.read_checkpoint(path) == NOW - 60
    assert len(calls) == 1


def test_trusted_save_repairs_bad_checkpoint_and_writes_only_boot_rtc(tmp_path, monkeypatch):
    path = tmp_path / "last-sync"
    path.write_text("99999999999")
    calls = []

    def run(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=REPORT)

    monkeypatch.setattr(clock.subprocess, "run", run)
    monkeypatch.setattr(clock.time, "time", lambda: NOW)
    clock.save(path)
    assert clock.read_checkpoint(path) == NOW
    assert calls[1] == [
        "/usr/sbin/hwclock", "--systohc", "--utc", "--noadjfile", "--rtc=/dev/rtc0"
    ]


def test_failed_atomic_replace_preserves_previous_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "last-sync"
    clock.write_checkpoint(path, NOW)

    def fail(*_):
        raise OSError("interrupted write")

    monkeypatch.setattr(clock.os, "replace", fail)
    with pytest.raises(OSError):
        clock.write_checkpoint(path, NOW + 60)
    assert clock.read_checkpoint(path) == NOW
    assert list(tmp_path.iterdir()) == [path]
