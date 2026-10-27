from __future__ import annotations

import math

import pytest

from codrive.hardware.camera import (
    GmslCameraConfig,
    UsbCameraConfig,
    build_gmsl_gstreamer,
    camera_source,
)
from codrive.hardware.novatel_ins import (
    EnuOrigin,
    NovatelInsParser,
    heading_to_enu_yaw,
    novatel_crc32,
    parse_bestposa,
    parse_gpgga,
    parse_heading2a,
)
from codrive.hardware.panda_can import PandaCanReceiver
from codrive.hardware.stm32_audio import AudioStreamDecoder, encode_audio_frame


BESTPOSA = (
    "#BESTPOSA,COM1,0,89.0,FINESTEERING,2420,111323.000,00000000,0001,781;"
    "SOL_COMPUTED,NARROW_INT,37.54221101204,127.07903112545,20.8976,23.2824,WGS84,0,0"
)
HEADING2A = (
    "#HEADING2A,COM1,0,89.0,FINESTEERING,2420,111323.000,00000000,0000,781;"
    "SOL_COMPUTED,NARROW_INT,0.996042550,96.204933167,-0.276466608,0.0,3.630645275"
)
GPGGA = "$GPGGA,065505.00,3732.5326607,N,12704.7418675,E,4,10,1.4,20.898,M,23.282,M,1.000,4095*73"
OFFICIAL_CRC_BESTPOSA = (
    "#BESTPOSA,COM1,0,78.0,FINESTEERING,1427,325298.000,00000000,6145,2748;"
    "SOL_COMPUTED,SINGLE,51.11678928753,-114.03886216575,1064.3470,-16.2708,"
    'WGS84,2.3434,1.3043,4.7300,"",0.000,0.000,7,7,0,0,0,06,0,03*9c9a92bb'
)


def test_pdf_novatel_examples_parse_and_combine() -> None:
    position = parse_bestposa(BESTPOSA)
    heading = parse_heading2a(HEADING2A)
    gga = parse_gpgga(GPGGA)

    assert position is not None
    assert position.latitude_deg == pytest.approx(37.54221101204)
    assert position.longitude_deg == pytest.approx(127.07903112545)
    assert heading is not None
    assert heading.heading_deg == pytest.approx(96.204933167)
    assert heading.enu_yaw_rad == pytest.approx(math.radians(-6.204933167))
    assert gga is not None
    assert gga.fix_quality == 4
    assert gga.satellites == 10
    assert gga.latitude_deg == pytest.approx(37.54221101166667)

    parser = NovatelInsParser(
        EnuOrigin(latitude_deg=37.54221101204, longitude_deg=127.07903112545, height_m=20.0)
    )
    assert parser.feed_line(BESTPOSA) is not None
    assert parser.feed_line(HEADING2A) is not None
    snapshot = parser.feed_line(GPGGA)
    assert snapshot is not None
    assert snapshot.east_m == pytest.approx(0.0, abs=1e-8)
    assert snapshot.north_m == pytest.approx(0.0, abs=1e-8)
    assert snapshot.up_m == pytest.approx(0.8976)
    assert snapshot.yaw_rad == pytest.approx(math.radians(-6.204933167))
    assert snapshot.fix_quality == 4


def test_novatel_malformed_lines_are_ignored_without_state_damage() -> None:
    parser = NovatelInsParser()
    valid = parser.feed_line(BESTPOSA)
    assert valid is not None
    assert parser.feed_line("#BESTPOSA,broken") is None
    assert parser.feed_line(GPGGA[:-2] + "00") is None
    assert parser.feed_line(b"\xff\xfe") is None
    assert parser.snapshot() == valid
    assert parse_bestposa(BESTPOSA.replace("37.54221101204", "nan")) is None
    assert heading_to_enu_yaw(0.0) == pytest.approx(math.pi / 2.0)


def test_novatel_crc_matches_official_example_and_rejects_corruption() -> None:
    message, expected = OFFICIAL_CRC_BESTPOSA[1:].split("*")
    assert novatel_crc32(message.encode("ascii")) == int(expected, 16)
    assert parse_bestposa(OFFICIAL_CRC_BESTPOSA) is not None
    assert parse_bestposa(OFFICIAL_CRC_BESTPOSA.replace("51.116", "51.117")) is None


def test_novatel_components_expire_independently() -> None:
    parser = NovatelInsParser(component_ttl_ms=500)
    start_ns = 1_000_000_000
    parser.feed_line(BESTPOSA, received_time_ns=start_ns)
    parser.feed_line(HEADING2A, received_time_ns=start_ns)

    updated = parser.feed_line(GPGGA, received_time_ns=start_ns + 600_000_000)

    assert updated is not None
    assert updated.position is None
    assert updated.heading is None
    assert updated.yaw_rad is None
    assert updated.gga is not None


def test_audio_decoder_handles_chunks_junk_crc_failure_and_resync() -> None:
    first_payload = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    second_payload = b"\x10\x00\x20\x00"
    first = encode_audio_frame(first_payload, sequence=41, timestamp_us=1_000_000)
    second = encode_audio_frame(second_payload, sequence=42, timestamp_us=1_000_500)
    corrupt = bytearray(first)
    corrupt[-1] ^= 0x80

    decoder = AudioStreamDecoder()
    assert decoder.feed(b"serial junk" + first[:11]) == ()
    decoded = decoder.feed(first[11:])
    assert len(decoded) == 1
    assert decoded[0].sequence == 41
    assert decoded[0].pcm16 == first_payload
    assert decoded[0].sample_count == 4

    decoded = decoder.feed(bytes(corrupt) + b"noise" + second)
    assert [frame.sequence for frame in decoded] == [42]
    assert decoder.crc_errors == 1
    assert decoder.bytes_discarded > 0


def test_audio_encoder_rejects_non_pcm16_or_wrong_format() -> None:
    with pytest.raises(ValueError):
        encode_audio_frame(b"\x00", sequence=0, timestamp_us=0)
    with pytest.raises(ValueError):
        encode_audio_frame(b"\x00\x00", sequence=0, timestamp_us=0, sample_rate_hz=48_000)


class _FakePanda:
    def __init__(self) -> None:
        self.closed = False

    def can_recv(self) -> list[object]:
        return [
            # Current Panda layout has no hardware timestamp.
            (0x123, b"\x01\x02", 0),
            # Legacy/test layout with a timestamp remains accepted.
            (0x18DAF110, 101, bytes(range(12)), 1),
            (0x124, 102, b"bad-size-11", 0),
            (0x125, 103, b"\x00", 0x80),
            ("bad", 104, b"\x00", 0),
        ]

    def close(self) -> None:
        self.closed = True


def test_panda_adapter_is_receive_only_and_filters_malformed_packets() -> None:
    device = _FakePanda()
    receiver = PandaCanReceiver(device)
    frames = receiver.read_available()
    assert [frame.arbitration_id for frame in frames] == [0x123, 0x18DAF110]
    assert frames[0].hardware_timestamp_us is None
    assert frames[1].hardware_timestamp_us == 101
    assert frames[1].is_extended
    assert frames[1].is_fd
    assert receiver.dropped_frames == 3
    assert not hasattr(receiver, "can_send")
    receiver.close()
    assert device.closed


def test_camera_sources_are_generated_without_hardware_imports() -> None:
    argus = build_gmsl_gstreamer(GmslCameraConfig(sensor_id=2, width=1280, height=720))
    assert "nvarguscamerasrc sensor-id=2" in argus
    assert "memory:NVMM" in argus

    v4l2 = build_gmsl_gstreamer(GmslCameraConfig(backend="v4l2", device="/dev/video3"))
    assert "v4l2src device=/dev/video3" in v4l2

    assert camera_source(UsbCameraConfig(use_gstreamer=False, device_index=4)) == 4
    assert "jpegdec" in camera_source(UsbCameraConfig(input_format="MJPG"))
