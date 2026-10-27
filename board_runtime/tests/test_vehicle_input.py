from dataclasses import replace
from types import SimpleNamespace

import pytest

from codrive.models import Header, VehicleState, from_json, to_json
from codrive.vehicle_input import DbcVehicleDecoder, VehicleWorker

NOW = 1500000000000000000


class Database:
    def decode_message(self, address, data, **kwargs):
        return {"speed": data[0], "pedal": data[0], "brake": data[0]}


def decoder():
    return DbcVehicleDecoder(
        {
            "required_signals": {
                "speed_mps": {"message_id": 10, "signal": "speed", "scale": 1 / 3.6},
                "accelerator_pct": {"message_id": 20, "signal": "pedal"},
                "brake_pressed": {"message_id": 30, "signal": "brake"},
            }
        },
        database=Database(),
    )


def frame(address, value, bus=0):
    return SimpleNamespace(arbitration_id=address, data=bytes([value]), bus=bus)


def test_each_signal_keeps_actual_reception_time_and_round_trips():
    d = decoder()
    first = d.decode(
        [frame(10, 36), frame(20, 50)], Header.now("pi5.panda", 1, capture_time_ns=NOW)
    )
    assert first.speed_mps == 10 and first.accelerator_pct == 50
    assert "brake_pressed" not in dict(first.signal_times_ns)
    second = d.decode([frame(10, 72)], Header.now("pi5.panda", 2, capture_time_ns=NOW + 100000000))
    assert dict(second.signal_times_ns) == {"speed_mps": NOW + 100000000, "accelerator_pct": NOW}
    assert from_json(VehicleState, to_json(second)) == second
    assert d.decode([frame(20, 70, bus=1)], Header.now("pi5.panda", 3, capture_time_ns=NOW)) is None


def test_brake_invalid_and_unknown_signals_are_not_verified():
    d = decoder()
    sample = d.decode(
        [frame(10, 36), frame(30, 2)], Header.now("pi5.panda", 1, capture_time_ns=NOW)
    )
    assert "brake_pressed" not in dict(sample.signal_times_ns)
    for value in (1, 0):
        sample = d.decode(
            [frame(30, value)], Header.now("pi5.panda", value + 2, capture_time_ns=NOW)
        )
        assert sample.brake_pressed is bool(value)


def test_missing_can_configuration_is_explicit(tmp_path):
    config = tmp_path / "can.yaml"
    config.write_text("receive_only: true\ndbc_path: null\n")
    worker = VehicleWorker(config)
    worker.start()
    assert worker.status()["status"] == "waiting_configuration"
    assert worker.take_latest() is None
    worker.close()


def test_invalid_signal_timestamp_is_rejected():
    sample = decoder().decode([frame(10, 36)], Header.now("pi5.panda", 1, capture_time_ns=NOW))
    with pytest.raises(ValueError):
        replace(sample, signal_times_ns=(("speed_mps", NOW + 1),))
