#!/usr/bin/env python3
"""Repeatable optical bench scenarios; CARLA ground truth is logged, never sent to DDS."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import signal
import threading
import time
import urllib.request
from pathlib import Path

LEGACY_ROLE = "codrive_metric_trial_20261025"
ROLE = "codrive_optical_scenario_v1"
KINDS = {"pedestrian": "walker.pedestrian.0001", "vehicle": "vehicle.audi.a2"}
PARKED = {"pedestrian": (12.0, -3.0), "vehicle": (16.0, -3.5)}


def trajectory(name, elapsed, speed):
    """Return forward/right metres and longitudinal closing speed; ego stays parked."""
    if not math.isfinite(speed) or not 0 < speed <= 8:
        raise ValueError("Target speed must be in (0, 8] m/s")
    if name == "crossing":
        duration = 6 / speed
        return 12.0, -3 + speed * min(max(elapsed, 0), duration), 0.0
    if name == "approach":
        duration = 9 / speed
        return 16 - speed * min(max(elapsed, 0), duration), 0.8, speed if 0 <= elapsed < duration else 0.0
    raise ValueError(f"Unknown scenario: {name}")


def motion_duration(name, speed):
    trajectory(name, 0, speed)
    return (6 if name == "crossing" else 9) / speed


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def pose(transform):
    p, r = transform.location, transform.rotation
    return {"x": p.x, "y": p.y, "z": p.z, "pitch": r.pitch, "yaw": r.yaw, "roll": r.roll}


def owned(actor, item):
    return actor is not None and actor.type_id == item["type_id"] and actor.attributes.get("role_name") == item["role"]


class Scene:
    def __init__(self, args, carla):
        self.args, self.carla = args, carla
        self.client = carla.Client(args.host, args.port)
        self.client.set_timeout(5)
        self.world = self.client.get_world()
        self.ego = self.world.get_actor(args.ego_id)
        if self.ego is None or not self.ego.type_id.startswith("vehicle."):
            raise RuntimeError("Existing ego vehicle not found; start the CARLA demo first")
        self.reference = self.ego.get_transform()
        self.assert_stationary()
        self.actors = {}
        self.state = None
        if args.state.exists():
            self.state = json.loads(args.state.read_text())
            if self.state["map"] != self.world.get_map().name or self.state["ego_id"] != self.ego.id:
                raise RuntimeError("Saved scene belongs to a different world/ego; inspect the state file")
            self.assert_reference(self.state["ego_pose"])
            for kind, item in self.state["actors"].items():
                actor = self.world.get_actor(item["id"])
                if not owned(actor, item):
                    raise RuntimeError(f"Saved {kind} missing or ownership changed; refusing to touch it")
                self.actors[kind] = actor

    def assert_reference(self, expected):
        current = pose(self.ego.get_transform())
        distance = math.sqrt(sum((current[k] - expected[k]) ** 2 for k in ("x", "y", "z")))
        rotation = max(abs((current[k] - expected[k] + 180) % 360 - 180) for k in ("pitch", "yaw", "roll"))
        if distance > .05 or rotation > .2:
            raise RuntimeError("Ego pose changed; stop and prepare a new calibrated setup")

    def assert_stationary(self):
        v = self.ego.get_velocity()
        if math.sqrt(v.x*v.x + v.y*v.y + v.z*v.z) > .05:
            raise RuntimeError("Optical bench scenario requires ego parked (<0.05 m/s)")

    def put(self, kind, forward, right):
        a = self.actors[kind]
        p = self.reference.transform(self.carla.Location(x=forward, y=right, z=0))
        # Use the same local plane as the active screen calibration; no map-wide depth claim.
        p.z = self.reference.location.z + (a.bounding_box.extent.z + .03 if kind == "pedestrian" else .15)
        a.set_transform(self.carla.Transform(p, self.carla.Rotation(yaw=self.reference.rotation.yaw + 180)))

    def prepare(self):
        if self.state is not None:
            self.reset()
            return
        created = []
        try:
            for kind, type_id in KINDS.items():
                # Adopt only the two explicitly labelled targets created earlier in this session.
                candidates = [a for a in self.world.get_actors().filter(type_id)
                              if a.attributes.get("role_name") in (LEGACY_ROLE, ROLE + "." + kind)]
                if len(candidates) > 1:
                    raise RuntimeError(f"Multiple owned {kind} targets; inspect before preparing")
                if candidates:
                    actor = candidates[0]
                else:
                    bp = self.world.get_blueprint_library().find(type_id)
                    bp.set_attribute("role_name", ROLE + "." + kind)
                    if bp.has_attribute("is_invincible"):
                        bp.set_attribute("is_invincible", "true")
                    f, r = PARKED[kind]
                    p = self.reference.transform(self.carla.Location(x=f, y=r, z=1.2))
                    actor = self.world.try_spawn_actor(bp, self.carla.Transform(p, self.reference.rotation))
                    if actor is None:
                        raise RuntimeError(f"Spawn blocked for {kind}")
                    created.append(actor)
                self.actors[kind] = actor
                actor.set_simulate_physics(False)
            self.state = {"schema_version": 1, "map": self.world.get_map().name,
                          "ego_id": self.ego.id, "ego_pose": pose(self.reference),
                          "actors": {k: {"id": a.id, "type_id": a.type_id,
                                         "role": a.attributes["role_name"]} for k, a in self.actors.items()}}
            self.reset()
            dump(self.args.state, self.state)
        except BaseException:
            for actor in created:
                actor.destroy()
            raise

    def reset(self):
        self.assert_stationary()
        for kind in self.actors:
            self.put(kind, *PARKED[kind])

    def cleanup(self):
        for kind, actor in self.actors.items():
            if not owned(actor, self.state["actors"][kind]):
                raise RuntimeError("Ownership changed; cleanup aborted")
        for actor in self.actors.values():
            if not actor.destroy():
                raise RuntimeError("Actor destroy failed; retain state for inspection")
        self.args.state.unlink(missing_ok=True)

    def truth(self, phase, closing):
        frame = self.world.get_snapshot()
        inverse = self.reference.get_inverse_matrix()
        targets = {}
        for kind, actor in self.actors.items():
            p = actor.get_location()
            coords = [sum(inverse[i][j] * v for j, v in enumerate((p.x, p.y, p.z, 1))) for i in range(3)]
            longitudinal, lateral = coords[:2]
            target_closing = closing if kind == "pedestrian" else 0.0
            targets[kind] = {"actor_id": actor.id, "forward_m": longitudinal, "right_m": lateral,
                             "horizontal_center_distance_m": math.hypot(longitudinal, lateral),
                             "longitudinal_closing_mps": target_closing,
                             "longitudinal_ttc_s": longitudinal / target_closing if target_closing > 0 else None,
                             "pose": pose(actor.get_transform())}
        return {"wall_time_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
                "carla_frame": frame.frame, "carla_elapsed_s": frame.timestamp.elapsed_seconds,
                "phase": phase, "ego_pose": pose(self.ego.get_transform()), "targets": targets}


class PiRecorder:
    def __init__(self, url, output):
        self.url, self.output = url.rstrip("/") + "/api/state", output
        self.stop = threading.Event()
        self.phase = "setup"
        self.thread = threading.Thread(target=self.record, daemon=True)
        self.count = self.errors = 0

    def record(self):
        with self.output.open("w") as out:
            while not self.stop.is_set():
                row = {"request_time_ns": time.time_ns(), "phase": self.phase}
                try:
                    with urllib.request.urlopen(self.url, timeout=2) as response:
                        s = json.load(response)
                    row.update({k: s.get(k) for k in ("updated_time_ns", "risk_assessment", "command", "speech", "rejected_samples")})
                    row["external"] = s.get("snapshot", {}).get("external", [])
                    row["vehicle"] = s.get("snapshot", {}).get("vehicle")
                    self.count += 1
                except Exception as exc:
                    row["error"] = str(exc)
                    self.errors += 1
                row["response_time_ns"] = time.time_ns()
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                self.stop.wait(.1)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)


def summarize(path):
    # Bound summary memory even if the user leaves a scene running overnight.
    from collections import deque
    rows = deque(maxlen=5000)
    total_rows = 0
    with path.open() as stream:
        for line in stream:
            rows.append(json.loads(line))
            total_rows += 1
    frames = {}
    levels, actions, speeches = set(), set(), set()
    for row in rows:
        risk = row.get("risk_assessment") or {}
        levels.add(risk.get("stable_level", "unavailable"))
        active = risk.get("active_risk") or {}
        if active.get("recommended_action"):
            actions.add(active["recommended_action"])
        speech = row.get("speech") or {}
        # Speech metadata can refer to an older run; retain its completion counter too.
        if speech.get("last_text"):
            speeches.add((speech.get("completed", 0), speech["last_text"]))
        for ext in row.get("external", []):
            h = ext.get("header", {})
            if ext.get("camera_id") == "front":
                frames.setdefault((h.get("boot_id"), h.get("sequence")), (row["phase"], ext))
    persons = [(phase, obj) for phase, ext in frames.values() for obj in ext.get("objects", [])
               if obj.get("class_name") in ("person", "pedestrian")]
    return {"pi_poll_samples": len(rows), "total_recorded_samples": total_rows, "summary_window_limit": 5000, "pi_poll_errors": sum("error" in row for row in rows),
            "unique_front_frames": len(frames), "pedestrian_detections": len(persons),
            "pedestrian_metric_samples": sum(obj.get("distance_m") is not None for _, obj in persons),
            "pedestrian_ttc_samples": sum(obj.get("ttc_s") is not None for _, obj in persons),
            "stationary_phase_ttc_samples": sum(phase in ("baseline", "hold") and obj.get("ttc_s") is not None
                                                 for phase, obj in persons),
            "stable_levels_observed": sorted(levels), "actions_observed": sorted(actions),
            "speech_metadata_observed": [{"completed": n, "text": text} for n, text in sorted(speeches)],
            "maximum_rejected_samples": max((r.get("rejected_samples", 0) or 0 for r in rows), default=0),
            "acceptance": "manual_review_required",
            "limitations": ["Camera was handheld during initial tests; metric/TTC accuracy is not accepted.",
                            "Phase labels use API request time, not aligned camera capture time; transition samples may lag.",
                            "TTC here is longitudinal approach time, not a lateral crossing collision predictor.",
                            "Ground truth is actor horizontal center; a vehicle box bottom may refer to its nearer surface.",
                            "Cached speech metadata does not by itself prove new playback or audible sound."]}


def run(scene, args):
    scene.prepare()
    duration = motion_duration(args.scenario, args.speed)
    args.output.mkdir(parents=True, exist_ok=False)
    recorder = PiRecorder(args.pi_url, args.output / "pi.jsonl")
    dump(args.output / "run.json", {"scenario": args.scenario, "speed_mps": args.speed,
         "baseline_s": args.baseline, "motion_s": duration, "hold_s": args.hold,
         "scene": scene.state, "method": "stationary ego; kinematic target motion; no CAN/INS/DDS test injection"})
    recorder.thread.start()
    completed, error = False, None
    phases = []
    try:
        with (args.output / "ground-truth.jsonl").open("w") as truth:
            scene.put("pedestrian", *trajectory(args.scenario, 0, args.speed)[:2])
            for phase, seconds in (("baseline", args.baseline), ("motion", duration), ("hold", args.hold)):
                recorder.phase = phase
                start = time.monotonic()
                phases.append({"phase": phase, "wall_time_ns": time.time_ns(), "planned_duration_s": seconds})
                print(f"{phase}: {seconds:.2f}s", flush=True)
                while True:
                    elapsed = time.monotonic() - start
                    if elapsed >= seconds:
                        break
                    scene.assert_stationary()
                    scene.assert_reference(scene.state["ego_pose"])
                    t = elapsed if phase == "motion" else (duration if phase == "hold" else -1)
                    forward, right, closing = trajectory(args.scenario, t, args.speed)
                    scene.put("pedestrian", forward, right)
                    truth.write(json.dumps(scene.truth(phase, closing)) + "\n")
                    truth.flush()
                    time.sleep(.05)
                if phase == "motion":
                    scene.put("pedestrian", *trajectory(args.scenario, duration, args.speed)[:2])
            completed = True
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        recorder.close()
        try:
            scene.reset()
        finally:
            dump(args.output / "phases.json", phases)
            report = summarize(args.output / "pi.jsonl")
            report.update({"run_completed": completed, "error": error})
            dump(args.output / "summary.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "reset", "cleanup"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--ego-id", type=int, default=45)
    parser.add_argument("--state", type=Path, default=Path("build/carla-scenarios/state.json"))
    parser.add_argument("--scenario", choices=("crossing", "approach"), default="crossing")
    parser.add_argument("--speed", type=float, default=2.0, help="Kinematic pedestrian speed, m/s")
    parser.add_argument("--baseline", type=float, default=4.0)
    parser.add_argument("--hold", type=float, default=4.0)
    parser.add_argument("--pi-url", default="http://192.168.99.21:8080")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    motion_duration(args.scenario, args.speed)
    if not all(math.isfinite(v) and 0 <= v <= 60 for v in (args.baseline, args.hold)):
        parser.error("Baseline/hold must be in [0, 60] seconds")
    if args.output is None:
        args.output = Path("build/carla-scenarios") / (time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}-{args.scenario}")
    import carla
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scene = Scene(args, carla)
        if args.action == "run":
            run(scene, args)
        elif args.action == "prepare":
            scene.prepare()
            print(json.dumps(scene.state, indent=2))
        elif scene.state is None:
            raise RuntimeError("No saved scene; prepare first")
        elif args.action == "reset":
            scene.reset()
        else:
            scene.cleanup()


if __name__ == "__main__":
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped; review the run summary and target reset state.")
        raise SystemExit(130)
