"""G29 raw monitor and measured calibration. Run with Python 3.12.

py -3.12 E:\\stm32\\g29_test.py --calibrate
Calibration: focus the window; hold each requested position and press Enter.
The axis numbers come from the measured changes, never from a default mapping.
"""

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import statistics
import time

CONFIG = Path(__file__).with_name("g29_axes.json")


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite calibration/input value")
    return value


def clamp(value, low, high):
    return max(low, min(high, value))


def validate_config(config, guid=None, num_axes=None):
    if config.get("version") != 1:
        raise ValueError("Unsupported calibration version; run --calibrate")
    if guid is not None and config.get("guid") != guid:
        raise ValueError("G29 GUID changed (driver/backend/device); recalibrate")
    axes = []
    for name in ("steering", "throttle", "brake"):
        item = config[name]
        axis = item["axis"]
        if type(axis) is not int or axis < 0 or (num_axes is not None and axis >= num_axes):
            raise ValueError(f"Invalid {name} axis index")
        axes.append(axis)
        if name == "steering":
            left, center, right = (finite(item[k]) for k in ("left", "center", "right"))
            if (left - center) * (right - center) >= 0 or min(abs(left-center), abs(right-center)) < 0.2:
                raise ValueError("Steering endpoints must straddle center with sufficient travel")
        elif abs(finite(item["pressed"]) - finite(item["rest"])) < 0.2:
            raise ValueError(f"Insufficient {name} travel")
    if len(set(axes)) != 3:
        raise ValueError("Combined/shared pedal axes are unsupported; enable separate pedals and recalibrate")
    return config


def load_config(path=CONFIG, guid=None, num_axes=None):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")), guid, num_axes)


def normalize(axes, config):
    s = config["steering"]
    raw = finite(axes[s["axis"]])
    delta = raw - s["center"]
    if delta * (s["right"] - s["center"]) >= 0:
        steering = 450 * clamp(delta / (s["right"] - s["center"]), 0, 1)
    else:
        steering = -450 * clamp(delta / (s["left"] - s["center"]), 0, 1)
    result = {"steering_deg": steering}
    for name in ("throttle", "brake"):
        p = config[name]
        result[name + "_pct"] = 100 * clamp(
            (finite(axes[p["axis"]]) - p["rest"]) / (p["pressed"] - p["rest"]), 0, 1)
    return result


class Wheel:
    def __init__(self, device_index=None):
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI", "0")
        os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")
        import pygame
        self.pg = pygame
        pygame.display.init()
        pygame.font.init()
        pygame.joystick.init()
        self.screen = pygame.display.set_mode((1000, 420))
        pygame.display.set_caption("CODRIVE G29 - calibration / input monitor")
        self.font = pygame.font.Font(None, 25)
        devices = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        print("Devices:", [(i, d.get_name(), d.get_guid()) for i, d in enumerate(devices)], flush=True)
        if device_index is not None:
            if not 0 <= device_index < len(devices):
                raise ValueError("Device index out of range")
            candidates = [devices[device_index]]
        else:
            candidates = [d for d in devices if "G29" in d.get_name().upper()]
        if len(candidates) != 1 or "G29" not in candidates[0].get_name().upper():
            raise ValueError("Connect one G29, or select its index using --device-index")
        self.joy = candidates[0]
        self.guid = self.joy.get_guid()
        self.count = self.joy.get_numaxes()
        self.enter = False

    def read(self):
        pg = self.pg
        self.enter = False
        for event in pg.event.get():
            if event.type == pg.QUIT:
                raise KeyboardInterrupt
            if event.type == pg.JOYDEVICEREMOVED and event.instance_id == self.joy.get_instance_id():
                raise RuntimeError("G29 disconnected. Stop; reconnect and restart (no stale values sent).")
            if event.type == pg.KEYDOWN:
                if event.key == pg.K_ESCAPE:
                    raise KeyboardInterrupt
                if event.key in (pg.K_RETURN, pg.K_KP_ENTER):
                    self.enter = True
        axes = [self.joy.get_axis(i) for i in range(self.count)]
        buttons = [i for i in range(self.joy.get_numbuttons()) if self.joy.get_button(i)]
        hats = [self.joy.get_hat(i) for i in range(self.joy.get_numhats())]
        return axes, buttons, hats

    def draw(self, lines):
        self.screen.fill((24, 28, 36))
        for row, line in enumerate(lines):
            self.screen.blit(self.font.render(str(line), True, (235, 240, 245)), (16, 16 + row * 35))
        self.pg.display.flip()

    def close(self):
        self.pg.quit()


def calibrate(wheel, path, overrides):
    steps = [
        ("center", "Wheel CENTER, release BOTH pedals. Hold steady, press ENTER."),
        ("left", "Wheel fully LEFT (-450 deg), pedals released. Hold, press ENTER."),
        ("right", "Wheel fully RIGHT (+450 deg), pedals released. Hold, press ENTER."),
        ("throttle_rest", "Wheel CENTER, release BOTH pedals. Hold, press ENTER."),
        ("throttle_full", "Press ONLY accelerator fully. Hold, press ENTER."),
        ("brake_rest", "Release BOTH pedals. Hold, press ENTER."),
        ("brake_full", "Press ONLY brake fully. Hold, press ENTER."),
    ]
    samples = {}
    for key, prompt in steps:
        print(prompt, flush=True)
        history = deque(maxlen=15)
        while True:
            axes, _, _ = wheel.read()
            history.append(axes)
            wheel.draw(["CALIBRATION - click window; ESC cancels", prompt,
                        "Raw: " + str([round(a, 3) for a in axes]),
                        "Keep position stable for at least 0.3 seconds before ENTER."])
            if wheel.enter and len(history) == history.maxlen:
                if any(max(col)-min(col) > 0.04 for col in zip(*history)):
                    print("Inputs unstable; hold steady and press ENTER again.", flush=True)
                else:
                    samples[key] = [statistics.median(col) for col in zip(*history)]
                    break
            time.sleep(0.02)

    def choose(name, low, high):
        changes = [abs(a-b) for a, b in zip(samples[low], samples[high])]
        axis = overrides.get(name)
        if axis is None:
            ranked = sorted(range(wheel.count), key=lambda i: changes[i], reverse=True)
            axis = ranked[0]
            if len(ranked) > 1 and changes[ranked[1]] > changes[axis] * 0.5:
                raise ValueError(f"Ambiguous {name} axis; repeat, or specify --{name}-axis INDEX")
        if not 0 <= axis < wheel.count or changes[axis] < 0.2:
            raise ValueError(f"No sufficient movement for {name}; retry calibration")
        return axis

    s = choose("steering", "left", "right")
    t = choose("throttle", "throttle_rest", "throttle_full")
    b = choose("brake", "brake_rest", "brake_full")
    config = {"version": 1, "guid": wheel.guid, "device_name": wheel.joy.get_name(),
              "steering": {"axis": s, **{k: samples[k][s] for k in ("left", "center", "right")}},
              "throttle": {"axis": t, "rest": samples["throttle_rest"][t], "pressed": samples["throttle_full"][t]},
              "brake": {"axis": b, "rest": samples["brake_rest"][b], "pressed": samples["brake_full"][b]}}
    validate_config(config, wheel.guid, wheel.count)
    print(json.dumps(config, indent=2), flush=True)
    print("Review measured axis numbers above. Focus window and press ENTER to save; ESC cancels.", flush=True)
    while True:
        wheel.read()
        wheel.draw(["Review measured calibration in terminal.", f"Steering axis {s}; throttle axis {t}; brake axis {b}",
                    "ENTER saves; ESC cancels. Full steering range must be 900 degrees in G HUB."])
        if wheel.enter:
            destination = Path(path)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            temporary.replace(destination)
            print("Saved:", destination, flush=True)
            return config
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--device-index", type=int)
    parser.add_argument("--seconds", type=float, default=0)
    for name in ("steering", "throttle", "brake"):
        parser.add_argument(f"--{name}-axis", type=int, help="Optional measured axis override during calibration")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds must be finite and nonnegative")
    wheel = None
    try:
        wheel = Wheel(args.device_index)
        if args.calibrate:
            config = calibrate(wheel, args.config, {n: getattr(args, n + "_axis") for n in ("steering", "throttle", "brake")})
        else:
            config = load_config(args.config, wheel.guid, wheel.count) if args.config.exists() else None
        started = time.monotonic()
        next_print = 0
        while not args.seconds or time.monotonic() - started < args.seconds:
            axes, buttons, hats = wheel.read()
            values = normalize(axes, config) if config else "RAW ONLY: run --calibrate to map axes"
            lines = ["G29 - move one control at a time; ESC exits", f"Raw indexed axes: {list(enumerate(round(a, 3) for a in axes))}",
                     f"Buttons: {buttons}; hats: {hats}", str(values)]
            wheel.draw(lines)
            if time.monotonic() >= next_print:
                print(" | ".join(lines[1:]), flush=True)
                next_print = time.monotonic() + 0.2
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("Stopped.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise SystemExit(f"G29 error: {error}") from error
    finally:
        if wheel:
            wheel.close()
        else:
            import sys
            if "pygame" in sys.modules:
                sys.modules["pygame"].quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"G29 program stopped: {error}") from error
