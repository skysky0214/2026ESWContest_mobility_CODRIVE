"""Seeded, time-ordered synthetic sensor sessions; never publishes sensor data."""

from __future__ import annotations

import random
from dataclasses import replace

from ..models import (
    AudioEvent,
    AudioKind,
    BoundingBox,
    DriverState,
    EnvironmentState,
    ExternalPerception,
    Header,
    SituationSnapshot,
    TrackedObject,
    TrafficLight,
    VehicleState,
    Weather,
)
from ..v3_feature_engine import RiskEngine

GENERATOR_VERSION = 1
SCENARIOS = (
    "normal",
    "objects",
    "stop_sign",
    "phone",
    "drowsy",
    "head_drop",
    "distracted",
    "siren",
    "horn",
    "red",
    "yellow",
    "rapid_acceleration",
    "rapid_pedal",
    "mixed",
)


def header(source, sequence, now, session):
    return Header(source, sequence, now, now, now, boot_id=f"synthetic-session-{session}")


def generate_sessions(*, samples, seed, rows_per_session, policy):
    if samples < 1000 or samples % rows_per_session or rows_per_session < 45:
        raise ValueError("Use >=1000 samples in whole sessions of at least 45 observations")
    n_sessions = samples // rows_per_session
    if n_sessions < 30:
        raise ValueError("At least 30 independent sessions are required")
    order = list(range(n_sessions))
    random.Random(seed ^ 0xA135).shuffle(order)
    train_end, val_end = int(n_sessions * 0.7), int(n_sessions * 0.85)
    splits = {
        sid: "train" if i < train_end else "validation" if i < val_end else "test"
        for i, sid in enumerate(order)
    }
    for sid in range(n_sessions):
        rng = random.Random(seed + sid * 1_000_003)
        scenario = SCENARIOS[sid % len(SCENARIOS)]
        engine = RiskEngine(policy=policy)
        base = 1_700_000_000_000_000_000 + sid * 3_600_000_000_000
        initial_speed = rng.choice([0.0, rng.uniform(0.5, 4), rng.uniform(4, 28)])
        longitudinal_acceleration = rng.uniform(2.5, 5.5)
        if scenario in {"red", "yellow", "rapid_acceleration", "mixed"}:
            initial_speed = rng.uniform(2, 22)
        weather = rng.choice(list(Weather)[1:])
        night = rng.random() < 0.4
        rain = rng.uniform(0.1, 25) if weather in {Weather.RAIN, Weather.THUNDER} else 0.0
        snow = rng.uniform(0.1, 4) if weather is Weather.SNOW else 0.0
        visibility = rng.uniform(60, 1500) if weather is Weather.FOG else rng.uniform(600, 15000)
        onset, release = rng.randint(0, 8), rng.randint(40, rows_per_session + 5)
        audio_direction = rng.choice(["left", "right"])
        n_objects = (
            rng.randint(1, 8) if scenario in {"objects", "mixed"} else rng.choice([0, 0, 1, 2])
        )
        road_objects = []
        for index in range(n_objects):
            width, height = rng.uniform(0.03, 0.5), rng.uniform(0.04, 0.7)
            x, y = rng.uniform(0, 1 - width), rng.uniform(0, 1 - height)
            road_objects.append(
                TrackedObject(
                    index,
                    rng.choice(["person", "car", "bus", "truck", "bicycle", "motorcycle"]),
                    rng.uniform(0.65, 0.99),
                    BoundingBox(x, y, x + width, y + height),
                )
            )
        if scenario == "stop_sign":
            road_objects.append(
                TrackedObject(99, "stop_sign", 0.95, BoundingBox(0.1, 0.1, 0.2, 0.3))
            )
        for frame in range(rows_per_session):
            now = base + frame * 100_000_000

            def h(source, sequence=frame + 1, stamp=now, session=sid):
                return header(source, sequence, stamp, session)

            active = onset <= frame < release
            behavior = (
                scenario
                if active and scenario in {"phone", "drowsy", "head_drop", "distracted"}
                else "awake"
            )
            if scenario == "mixed" and active:
                behavior = (
                    rng.choice(["phone", "drowsy", "distracted"]) if frame == onset else "phone"
                )
            speed = initial_speed
            if scenario == "rapid_acceleration":
                speed += longitudinal_acceleration * frame / 10
            elif speed > 0:
                speed = max(0, speed + 0.3 * frame / 10 + rng.uniform(-0.03, 0.03))
            pedal = rng.uniform(5, 45) if speed else 0.0
            if scenario == "rapid_pedal":
                pedal = [0, 0, 0, 35, 70, 100, 100, 70, 30, 0, 0, 0][frame % 12]
            elif scenario == "rapid_acceleration":
                pedal = rng.uniform(65, 95)
            brake = (
                bool(rng.random() < 0.12)
                if scenario not in {"rapid_acceleration", "rapid_pedal"}
                else False
            )
            times = tuple((name, now) for name in ("speed_mps", "accelerator_pct", "brake_pressed"))
            vehicle = VehicleState(
                h("synthetic.vehicle"), min(speed, 110), 0, brake, pedal, signal_times_ns=times
            )
            driver = DriverState(
                h("synthetic.driver"),
                "cabin",
                "synthetic-v1",
                attention_score=rng.uniform(0.02, 0.55)
                if behavior != "awake"
                else rng.uniform(0.7, 1),
                looking_forward=behavior == "awake",
                phone_detected=behavior == "phone",
                drowsiness_score=rng.uniform(0.65, 1)
                if behavior in {"drowsy", "head_drop"}
                else rng.uniform(0, 0.3),
                behavior=behavior,
            )
            kind = (
                AudioKind(scenario) if scenario in {"siren", "horn"} and active else AudioKind.NONE
            )
            if scenario == "mixed" and active:
                kind = AudioKind.SIREN
            if scenario == "normal" and sid % 3 == 0:
                kind = AudioKind.OTHER
            audio = AudioEvent(
                h("synthetic.audio"),
                kind,
                0.95,
                960,
                "synthetic-v1",
                audio_direction if kind in {AudioKind.SIREN, AudioKind.HORN} else "unknown",
            )
            light = (
                TrafficLight.RED
                if scenario in {"red", "mixed"}
                else TrafficLight.YELLOW
                if scenario == "yellow"
                else TrafficLight.GREEN
                if sid % 3 == 0
                else TrafficLight.OFF
                if sid % 3 == 1
                else TrafficLight.UNKNOWN
            )
            if not active:
                light = TrafficLight.GREEN
            front = ExternalPerception(
                h("synthetic.front"),
                "front",
                "synthetic-v1",
                objects=tuple(road_objects),
                traffic_light=light,
                traffic_light_confidence=0.95,
            )
            env = EnvironmentState(
                h("synthetic.environment"),
                weather=weather,
                is_night=night,
                precipitation_mm_h=rain,
                snowfall_cm_h=snow,
                visibility_m=visibility,
                api_valid=True,
                daylight_valid=True,
                weather_time_ns=base,
                weather_valid_until_ns=base + 4_500_000_000_000,
            )
            # Contiguous outages and per-signal omissions, with no normal-value substitution.
            outage = sid % 8
            if sid % 4 != 0 and 18 <= frame <= 25:
                if outage == 1:
                    front = None
                elif outage == 2:
                    driver = None
                elif outage == 3:
                    audio = None
                elif outage == 5:
                    vehicle = None
                elif outage == 6:
                    env = replace(env, api_valid=False)
                elif outage == 7:
                    env = replace(env, daylight_valid=False)
            if vehicle is not None and sid % 5 == 0 and 30 <= frame <= 35:
                vehicle = replace(vehicle, signal_times_ns=(("speed_mps", now),))
            if driver is not None and sid % 11 == 0 and 26 <= frame <= 28:
                driver = replace(driver, face_detected=False)
            # A packet may be present but expired; the common extractor decides validity.
            if front is not None and sid % 9 == 0 and 35 <= frame <= 37:
                front = replace(
                    front, header=replace(front.header, capture_time_ns=now - 300_000_000)
                )
            if env is not None and sid % 13 == 0 and frame >= 30:
                env = replace(env, weather_valid_until_ns=now)
            snap = SituationSnapshot(
                h("synthetic.fusion"),
                vehicle=vehicle,
                driver=driver,
                audio=audio,
                external=() if front is None else (front,),
                environment=env,
            )
            engine.evaluate(snap, now_ns=now)
            severity = max((int(c.severity) for c in engine.last_candidates), default=0)
            label = "LOW" if severity == 0 else "MEDIUM" if severity == 1 else "HIGH"
            yield {
                "session_id": f"session-{sid:04d}",
                "frame": frame,
                "time_ns": now,
                "scenario": scenario,
                "split": splits[sid],
                "target": label,
                "rule_codes": sorted({c.code for c in engine.last_candidates}),
                "rule_severity": severity,
                "inputs": dict(engine.last_features),
                "snapshot": snap,
            }
