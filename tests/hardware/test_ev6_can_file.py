from __future__ import annotations

from codrive.hardware.can.ev6_file import Ev6CanFileDecoder, grouped_can_jsonl
from codrive.hardware.can.panda import CanFrame
from codrive.contracts.models import Header


def test_ev6_decoder_extracts_vehicle_state_from_injected_frames() -> None:
    wheel = bytes(8) + bytes((128, 2)) * 4  # 20 km/h on each wheel
    steering = bytes((0, 0, 0, 100, 0, 0))  # +10.0 degrees
    accelerator = bytes((0, 0, 0, 0, 0, 127))
    tcs = bytes(9) + bytes((4, 0, 0))  # brake light asserted; 12-byte CAN-FD payload
    state = Ev6CanFileDecoder().decode(
        (
            CanFrame(160, wheel, 0),
            CanFrame(293, steering, 0),
            CanFrame(53, accelerator, 0),
            CanFrame(373, tcs, 0),
        ),
        Header.now("file.can", 1, capture_time_ns=1_000_000_000),
    )
    assert round(state.speed_mps, 3) == 5.556
    assert state.steering_wheel_deg == 10.0
    assert round(state.accelerator_pct, 1) == 49.8
    assert state.brake_pressed and state.can_valid


def test_grouped_jsonl_preserves_timestamp_batches(tmp_path) -> None:
    path = tmp_path / "can.jsonl"
    path.write_text(
        '{"ts":1.0,"address":160,"bytes":"00000000000000000000000000000000","bus":0}\n'
        '{"ts":1.0,"address":293,"bytes":"000000000000","bus":0}\n'
        '{"ts":2.0,"address":53,"bytes":"000000000000","bus":0}\n'
    )
    batches = list(grouped_can_jsonl(path))
    assert [len(frames) for _, frames in batches] == [2, 1]
    assert batches[0][0] == 1_000_000_000
