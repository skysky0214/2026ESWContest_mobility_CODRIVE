"""Offline contract tests: py -3.12 -m unittest -v test_g29_can (in E:\\stm32)."""

import copy
import struct
import unittest

from g29_can_sender import BrakeSwitch, encode_frames
from g29_test import normalize, validate_config
from g29_ecu_monitor import parse_telemetry, format_telemetry


class ContractTests(unittest.TestCase):
    def setUp(self):
        # Synthetic calibration fixture only, never written as device calibration.
        self.config = {"version": 1, "guid": "fixture",
                       "steering": {"axis": 2, "left": -0.9, "center": 0.1, "right": 0.95},
                       "throttle": {"axis": 0, "rest": 1.0, "pressed": -1.0},
                       "brake": {"axis": 4, "rest": -1.0, "pressed": 1.0}}

    def test_wire_examples(self):
        frames = dict(encode_frames(12.34, True, -45.6))
        self.assertEqual(frames[0x100], bytes.fromhex("D2 04 00 00 00 00 00 00"))
        self.assertEqual(frames[0x101], bytes.fromhex("01 00 00 00 00 00 00 00"))
        self.assertEqual(frames[0x102], bytes.fromhex("38 FE 00 00 00 00 00 00"))

    def test_endpoints_roundtrip(self):
        for speed in (0, 12.34, 655.35):
            for steering in (-450, -0.1, 0, 0.1, 450):
                for brake in (False, True):
                    frames = dict(encode_frames(speed, brake, steering))
                    self.assertEqual(set(frames), {0x100, 0x101, 0x102})
                    self.assertTrue(all(len(d) == 8 for d in frames.values()))
                    self.assertAlmostEqual(struct.unpack("<H", frames[0x100][:2])[0] * .01, speed)
                    self.assertAlmostEqual(struct.unpack("<h", frames[0x102][:2])[0] * .1, steering)
                    self.assertEqual(frames[0x101][0], int(brake))

    def test_invalid_signals(self):
        for args in ((-1, 0, 0), (656, 0, 0), (0, 2, 0), (0, 0, 451),
                     (float("nan"), 0, 0), (0, 0, float("inf"))):
            with self.assertRaises(ValueError):
                encode_frames(*args)

    def test_measured_axes_center_and_pedal_polarity(self):
        validate_config(self.config, "fixture", 6)
        rest = normalize([1, 0, .1, 0, -1, 0], self.config)
        self.assertEqual(rest, {"steering_deg": 0, "throttle_pct": 0, "brake_pct": 0})
        full = normalize([-1, 0, -.9, 0, 1, 0], self.config)
        self.assertEqual(full, {"steering_deg": -450, "throttle_pct": 100, "brake_pct": 100})
        self.assertEqual(normalize([0, 0, .95, 0, 0, 0], self.config)["steering_deg"], 450)

    def test_reversed_steering(self):
        config = copy.deepcopy(self.config)
        config["steering"].update(left=.95, right=-.9)
        validate_config(config)
        self.assertEqual(normalize([1, 0, -.9, 0, -1, 0], config)["steering_deg"], 450)
        self.assertEqual(normalize([1, 0, .95, 0, -1, 0], config)["steering_deg"], -450)

    def test_shared_or_flat_axes_rejected(self):
        for change in ("shared", "flat", "wrong_guid"):
            config = copy.deepcopy(self.config)
            if change == "shared":
                config["brake"]["axis"] = 0
            elif change == "flat":
                config["brake"]["pressed"] = config["brake"]["rest"]
            else:
                config["guid"] = "different"
            with self.assertRaises(ValueError):
                validate_config(config, "fixture", 6)

    def test_brake_hysteresis(self):
        switch = BrakeSwitch(5, 2)
        self.assertEqual([switch.update(v) for v in (0, 4, 5, 4, 3, 2, 0)],
                         [False, False, True, True, True, False, False])

    def test_actual_ecu_telemetry(self):
        line = ("ECU,ms=1000,speed_raw=1234,brake=1,steer_raw=-456,rx=3,valid=3,"
                "speed_n=1,brake_n=1,steer_n=1,speed_ms=900,brake_ms=901,steer_ms=902,"
                "err=0,invalid=0,lost=0,bo=0,lec=0\r\n")
        values = parse_telemetry(line)
        self.assertIn("ECU LIVE", format_telemetry(values))
        self.assertIn("12.34", format_telemetry(values))
        self.assertIn("-45.6", format_telemetry(values))
        values["ms"] = 2100
        self.assertIn("STALE", format_telemetry(values))
        values["ms"] = 1000
        values["brake_n"] = 0
        self.assertIn("STALE", format_telemetry(values))

    def test_incomplete_telemetry_rejected(self):
        with self.assertRaises((ValueError, KeyError)):
            parse_telemetry("ECU,ms=1000,speed_raw=12")
        with self.assertRaises(ValueError):
            parse_telemetry("PC decode: speed=12.34")


if __name__ == "__main__":
    unittest.main()
