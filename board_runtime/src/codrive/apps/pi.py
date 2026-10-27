"""Pi live DDS receiver, existing risk engine, browser HMI and local Korean TTS."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from contextlib import nullcontext as _NullContext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import latency_probe as latency
from ..environment import EnvironmentPolicy, EnvironmentWorker
from ..fusion import FusionPolicy, SituationFusion
from ..guidance import HmiArbiter
from ..health import HealthPolicy, NodeHealthMonitor
from ..models import Header, HmiCommand, NodeHealth, NodeState, to_json
from ..observation_speech import ObservationSpeechGate
from ..risk import RiskEngine, RiskPolicy, load_risk_policy
from ..runtime import PiOrchestrator, publish_model
from ..speech import DEFAULT_CACHE_DIR, SpeechCache, physical_sink
from ..transport import CycloneDdsBus
from ..vehicle_input import VehicleWorker
from ..warning_display import WarningDisplay
from .main_hmi import document as main_hmi_document

LOG = logging.getLogger("codrive.pi")


def vars_candidate(candidate):
    from dataclasses import asdict
    value = asdict(candidate)
    value["level"] = candidate.level.value
    value["severity"] = candidate.level.severity.name
    return value


class LocalSpeech:
    """One bounded speech worker; expired queued commands are never played."""

    def __init__(self, enabled: bool = False, *, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> None:
        self.enabled = enabled
        self.cache = SpeechCache(cache_dir)
        self.pending: queue.Queue[HmiCommand | None] = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self._playing_command = None
        self._process = None
        self._interrupted = False
        self.state: dict[str, Any] = {
            "enabled": enabled, "completed": 0, "error": None,
            "engine": "supertonic-3", "voice": "F1", "language": "ko", "cache_ready": False,
            "playing": False,
        }
        self.thread = threading.Thread(target=self._run, daemon=True)
        if enabled:
            try:
                self.cache.verify_templates()
                self.state["cache_ready"] = True
            except (OSError, ValueError) as exc:
                self.state["error"] = str(exc)
            self.thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def current_command(self):
        with self.lock:
            return self._playing_command

    @staticmethod
    def priority(command):
        if command.risk_code in {"pedestrian_collision", "object_collision"} and int(command.severity)>=2:
            return 4
        return int(command.severity)

    def submit(self, command: HmiCommand) -> None:
        if not self.enabled or command.expires_at_ns<=time.time_ns():
            return
        with self.lock:
            active=self._playing_command
            if active and not self._interrupted:
                if active.event_id==command.event_id or self.priority(command)<self.priority(active):
                    return
            try:
                queued=self.pending.get_nowait()
            except queue.Empty:
                queued=None
            if queued and queued.expires_at_ns>time.time_ns() and self.priority(queued)>self.priority(command):
                self.pending.put_nowait(queued)
                return
            self.pending.put_nowait(command)
            if active and self._process is not None and not self._interrupted and self.priority(command)>self.priority(active):
                self._interrupted=True
                self.state["interrupted"]=self.state.get("interrupted",0)+1
                try:self._process.terminate()
                except ProcessLookupError:pass
                LOG.info("tts_preempt old=%s new=%s",active.risk_code,command.risk_code)

    def _run(self) -> None:
        while True:
            command = self.pending.get()
            if command is None:
                return
            if command.expires_at_ns <= time.time_ns():
                continue
            try:
                preparation_start = time.time_ns()
                sink = physical_sink()
                with self.lock:
                    self.state["sink"] = sink
                wave = self.cache.wave_for(command.tts_text)
                last_engine = "supertonic-3"

                if command.expires_at_ns <= time.time_ns():
                    continue
                with self.lock:
                    # A higher-priority command may have arrived during sink/cache preparation.
                    with self.pending.mutex if isinstance(self.pending, queue.Queue) else _NullContext():
                        waiting=list(self.pending.queue) if isinstance(self.pending, queue.Queue) else []
                    if any(c is not None and c.expires_at_ns>time.time_ns() and self.priority(c)>self.priority(command) for c in waiting):
                        continue
                    process=subprocess.Popen(
                        ["paplay", "--client-name=CODRIVE", "--device="+sink],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self._process=process
                    self._interrupted=False
                    self._playing_command = command
                    self.state.update(playing=True, playing_event_id=command.event_id)
                playback_call = time.time_ns()
                latency.record('speech_start_call', {'event_id':command.event_id,
                    'command_created_ns':command.header.capture_time_ns,
                    'queue_prepare_s':(playback_call-command.header.capture_time_ns)/1e9,
                    'prepare_s':(playback_call-preparation_start)/1e9})
                try:
                    _,stderr=process.communicate(input=wave,timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
                with self.lock:
                    interrupted=self._interrupted
                if interrupted:
                    latency.record('speech_interrupted', {'event_id':command.event_id})
                    continue
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode,process.args,stderr=stderr)
                with self.lock:
                    self.state.update(completed=self.state["completed"] + 1, error=None,
                                      last_text=command.tts_text, cache_ready=True, last_engine=last_engine)
                latency.record('speech_complete', {'event_id':command.event_id,
                               'playback_call_ns':playback_call})
                LOG.info("tts_completed event=%s", command.event_id)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                with self.lock:
                    self.state["error"] = str(exc)
                LOG.error("tts_failed: %s", exc)
            finally:
                with self.lock:
                    self._playing_command = None
                    self._process=None
                    self._interrupted=False
                    self.state.update(playing=False, playing_event_id=None)


class DashboardState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: dict[str, Any] = {"status": "starting"}

    def update(self, value: dict[str, Any]) -> None:
        with self.lock:
            self.value = value

    def read(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.value)


def make_http_server(host: str, port: int, state: DashboardState) -> ThreadingHTTPServer:
    page = Path(__file__).with_name("dashboard.html").read_bytes()
    script = Path(__file__).with_name("dashboard.js").read_bytes()
    main_page = main_hmi_document()
    live_script_path = Path(__file__).with_name("main_hmi_live.js")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if (self.path != '/api/latency-render' or not latency.enabled()
                    or self.client_address[0] not in ('127.0.0.1','::1')):
                self.send_error(404)
                return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 4096: raise ValueError('size')
                value=json.loads(self.rfile.read(size))
                if not isinstance(value,dict): raise ValueError('object')
                latency.record('browser_render', value)
            except (ValueError,TypeError):
                self.send_error(400)
                return
            self.send_response(204); self.end_headers()

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if self.path in ("/api/state", "/api/hmi"):
                value = state.read()
                if self.path == "/api/hmi":
                    value = {key: value.get(key) for key in (
                        "updated_time_ns", "snapshot", "command", "nodes", "speech", "risk_model", "latency_probe", "test_mode", "vehicle_input",
                        "camera_preview", "environment_input", "scenario_context", "audio_input", "rule_risk",
                        "risk_assessment", "driver_attention", "vehicle_dynamics", "vehicle_ttl_ms", "driving_corridor",
                    )}
                    value["server_time_ns"] = time.time_ns()
                content = json.dumps(value, ensure_ascii=False).encode()
                kind = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                content, kind = main_page, "text/html; charset=utf-8"
            elif self.path == "/diagnostics":
                content, kind = page, "text/html; charset=utf-8"
            elif self.path == "/main_hmi_live.js":
                content, kind = live_script_path.read_bytes(), "text/javascript; charset=utf-8"
            elif self.path == "/dashboard.js":
                content, kind = script, "text/javascript; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain-id", type=int, default=42)
    parser.add_argument("--dds-config", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--tts-cache", type=Path, default=DEFAULT_CACHE_DIR)
    audio_options = parser.add_mutually_exclusive_group()
    audio_options.add_argument("--audio-alerts", action="store_true", dest="audio_alerts", default=True)
    audio_options.add_argument("--no-audio-alerts", action="store_false", dest="audio_alerts")
    parser.add_argument("--risk-config", type=Path, default=Path("config/risk.yaml"))
    parser.add_argument("--driving-corridor-config", type=Path, default=Path("config/camera/carla-driving-corridor.json"))
    parser.add_argument("--no-driving-corridor", action="store_true")
    parser.add_argument("--environment-config", type=Path, default=Path("config/environment/pi5.yaml"))
    parser.add_argument("--latitude", type=float, default=os.environ.get("CODRIVE_LATITUDE"))
    parser.add_argument("--longitude", type=float, default=os.environ.get("CODRIVE_LONGITUDE"))
    parser.add_argument("--no-environment", action="store_true")
    parser.add_argument("--can-config", type=Path, default=Path("config/can/ev6_2022.example.yaml"))
    parser.add_argument("--duration", type=float)
    parser.add_argument("--front-camera-stream", default=os.environ.get("CODRIVE_FRONT_CAMERA_STREAM"),
                        help="Optional live MJPEG preview URL; does not enable perception inputs")
    parser.add_argument("--cabin-camera-stream", default=os.environ.get("CODRIVE_CABIN_CAMERA_STREAM"),
                        help="Optional cabin MJPEG URL from the existing DMS capture")
    parser.add_argument("--xgboost-model", type=Path,
                        help="Optional B/v2 or final-sensors-v3 directory; HMI display only")
    parser.add_argument('--vehicle-source', choices=('panda', 'can-ecu', 'socketcan', 'eq900-obd'), default='panda',
                        help='Select local Panda or external STM32 DDS vehicle input')
    parser.add_argument('--vehicle-ttl-ms', type=int, default=None,
                        help='Vehicle freshness; defaults to 500 ms for CAN ECU, 120 ms for Panda')
    parser.add_argument('--external-ttl-ms', type=int, default=250,
                        help='Bounded camera capture age; CARLA relay demo uses 500 ms')
    parser.add_argument('--screen-recapture', action='store_true', help='Label the physical-camera CARLA screen demo')
    parser.add_argument('--real-vehicle-test', action='store_true', help='Reject simulation environment and G29 indicator injection')
    args = parser.parse_args(argv)
    if not 1 <= args.external_ttl_ms <= 1000:
        parser.error('--external-ttl-ms must be between 1 and 1000')
    latency.install_pi()
    vehicle_ttl_ms = args.vehicle_ttl_ms if args.vehicle_ttl_ms is not None else (750 if args.vehicle_source == 'eq900-obd' else 500 if args.vehicle_source == 'can-ecu' else 120)
    if vehicle_ttl_ms <= 0:
        parser.error('--vehicle-ttl-ms must be positive')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    shadow = None
    model_state: dict[str, Any] = {"status": "disabled", "mode": "display_only"}
    if args.xgboost_model:
        try:
            from ..risk_ml.infer import Predictor
            from ..risk_ml.shadow import ShadowRisk
            shadow = ShadowRisk(Predictor(args.xgboost_model))
        except Exception as exc:
            LOG.exception("XGBoost unavailable")
            model_state = {"status": "model_error", "mode": "display_only", "error": str(exc)}
    import yaml
    env_options = yaml.safe_load(args.environment_config.read_text())
    if not isinstance(env_options, dict):
        parser.error("environment config must be a mapping")
    if args.latitude is not None or args.longitude is not None:
        env_options.update(latitude=args.latitude, longitude=args.longitude)
    if args.no_environment:
        env_options["enabled"] = False
    environment = EnvironmentWorker(EnvironmentPolicy(**env_options))
    from ..driving_corridor import DrivingCorridor
    driving_corridor = None if args.no_driving_corridor else DrivingCorridor.load(args.driving_corridor_config)
    team_policy = load_risk_policy(args.risk_config)
    risk_policy = RiskPolicy(
        hard_acceleration_mps2=team_policy.hard_acceleration_mps2,
        accelerator_mismatch_pct=team_policy.accelerator_mismatch_pct,
        acceleration_confirm_ms=team_policy.acceleration_confirm_ms,
        cabin_observation_enabled=True,
        audio_alerts_enabled=args.audio_alerts,
        can_speed_acceleration_enabled=args.vehicle_source == "can-ecu",
        full_pedal_warning_enabled=args.real_vehicle_test and args.vehicle_source == "eq900-obd")
    os.environ["CYCLONEDDS_URI"] = Path(args.dds_config).resolve().as_uri()
    bus = CycloneDdsBus(domain_id=args.domain_id)
    orchestrator = PiOrchestrator(
        bus,
        fusion=SituationFusion(policy=FusionPolicy(
            expected_camera_ids=("front",),
            allow_scenario_context=not args.real_vehicle_test,
            environment_ttl_ms=3000,
            vehicle_ttl_ms=vehicle_ttl_ms,
            external_ttl_ms=args.external_ttl_ms,
            source_allowlist=(("driver:cabin", "jetson-cabin.dms"),
                              ("turn_indicator", "disabled-in-real-vehicle" if args.real_vehicle_test else "laptop.g29.indicators"),
                              ("vehicle", "pi5.can_ecu" if args.vehicle_source == "can-ecu" else "pi5.socketcan" if args.vehicle_source == "socketcan" else "pi5.eq900_obd" if args.vehicle_source == "eq900-obd" else "pi5.panda"), ("ins", "pi5.ins"),
                              ("audio", "jetson-cabin.audio"),
                              ("external:front", "jetson-front.front"),
                              ("environment", "pi5.environment")),
        )),
        risk_engine=RiskEngine(policy=risk_policy, team_policy=team_policy, vehicle_ttl_ms=vehicle_ttl_ms, external_ttl_ms=args.external_ttl_ms, driving_corridor=driving_corridor),
        health_monitor=NodeHealthMonitor(policy=HealthPolicy(
            expected_nodes=("jetson-front", "jetson-cabin.link", "jetson-cabin",
                            "jetson-cabin.audio"),
        )),
    )
    if shadow is not None and shadow.final_adapter is not None:
        from dataclasses import replace
        shadow.final_adapter.engine.policy = replace(
            shadow.final_adapter.engine.policy, vehicle_ttl_ms=vehicle_ttl_ms,
            external_ttl_ms=args.external_ttl_ms)
    state = DashboardState()
    server = make_http_server(args.host, args.port, state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    speech = LocalSpeech(args.tts, cache_dir=args.tts_cache)
    arbiter = HmiArbiter(repeat_speech_gap_ms=10_000)
    observation_speech = ObservationSpeechGate()
    warning_display = WarningDisplay()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    started = time.monotonic()
    next_tick = started
    next_log = started
    next_health = started
    health_sequence = 0
    active_command: HmiCommand | None = None
    recent_events: deque[dict[str, Any]] = deque(maxlen=20)
    last_driver = None
    observed = 0
    receive_age_ms: deque[float] = deque(maxlen=1000)
    dispatch_age_ms: deque[float] = deque(maxlen=1000)
    vehicle_input = VehicleWorker(args.can_config) if args.vehicle_source in ("panda", "socketcan") else None
    if args.vehicle_source=='eq900-obd':
        from ..eq900_obd import Eq900ObdWorker
        vehicle_input=Eq900ObdWorker(args.can_config)
    if vehicle_input is not None:
        vehicle_input.start()
    environment.start()
    last_position_check = 0
    LOG.info("HMI listening on port %d; domain=%d; team FSM/TTC risk enabled", args.port, args.domain_id)
    try:
        while not stop.is_set():
            now = time.time_ns()
            trace = latency.begin("pi_cycle")
            vehicle_sample = vehicle_input.take_latest() if vehicle_input is not None else None
            if vehicle_sample is not None:
                accepted=orchestrator.fusion.ingest(vehicle_sample, received_time_ns=now)
                if not accepted:LOG.warning("local_vehicle_rejected source=%s sequence=%s",vehicle_sample.header.source_id,vehicle_sample.header.sequence)
            env_sample = environment.take_latest()
            if env_sample is not None:
                orchestrator.fusion.ingest(env_sample, received_time_ns=now)
            result = orchestrator.step(now_ns=now)
            snapshot = result.snapshot
            if now-last_position_check >= 1_000_000_000:
                environment.update_position(snapshot.ins, now)
                last_position_check = now
            if shadow is not None:
                model_state = shadow.update(
                    snapshot, now_ns=now)
            driver = snapshot.driver
            observation_speech.update(driver, now_ns=now)
            if driver:
                identity = (driver.header.boot_id, driver.header.sequence)
                if identity != last_driver:
                    observed += 1
                    last_driver = identity
                    receive_age_ms.append((now - driver.header.publish_time_ns) / 1e6)
                    dispatch_age_ms.append((time.time_ns() - driver.header.capture_time_ns) / 1e6)
            assessment = orchestrator.risk_engine.assessment
            if active_command and assessment is not None:
                active = assessment.active_risk
                if (active is None or active.code != active_command.risk_code
                        or active.level.severity.value != active_command.severity.value):
                    active_command = None
                    arbiter.clear_active()
            if result.hmi:
                if latency.enabled():
                    latency.record('warning_created', {'event_id':result.hmi.event_id,
                        'risk_code':result.hmi.risk_code,
                        'inputs':[latency.header_key(e.header) for e in snapshot.external] +
                                 ([latency.header_key(snapshot.driver.header)] if snapshot.driver else []),
                        'created_ns':result.hmi.header.capture_time_ns})
                decision = arbiter.accept(result.hmi, now_ns=now,
                                         allow_speech=observation_speech.eligible(result.hmi, now_ns=now))
                if decision:
                    active_command = decision.command
                    recent_events.appendleft(json.loads(to_json(result.risk)))
                    if decision.speak:
                        speech.submit(decision.command)
                        observation_speech.mark_spoken(decision.command)
                    LOG.info("hmi_dispatch code=%s severity=%s text=%s", result.hmi.risk_code,
                             result.hmi.severity.name, result.hmi.display_text)
            if active_command and active_command.expires_at_ns <= now:
                active_command = None
            cabin_commands = {"driver_attention_observation": "distracted",
                              "driver_drowsy_observation": "drowsy", "driver_inattention": "distracted",
                              "driver_phone_observation": "phone"}
            attention_state = orchestrator.risk_engine.driver_attention.status()
            if active_command and active_command.risk_code in cabin_commands:
                behavior = attention_state["display_behavior"]
                if (behavior != cabin_commands[active_command.risk_code] and
                        not (active_command.risk_code == "driver_inattention" and behavior == "phone")):
                    active_command = None
            display_command = warning_display.update(
                active_command, speech.current_command(), now_ns=now)
            state.update({
                "status": "live" if driver or snapshot.audio or snapshot.vehicle or snapshot.external else "waiting",
                "updated_time_ns": now,
                "latency_probe": latency.enabled(),
                "uptime_s": round(time.monotonic() - started, 1),
                "domain_id": args.domain_id,
                "dds_network": "wired",
                "scope": "team_fsm_ttc",
                "driving_corridor": driving_corridor.status if driving_corridor else {"status":"disabled"},
                "environment_risk": {"weight": orchestrator.risk_engine.environment_context[0],
                                     "evidence": orchestrator.risk_engine.environment_context[1]},
                "scenario_context": ({"id": context.scenario_id, "source": context.header.source_id,
                                      "weather_override": context.environment is not None}
                                     if (context := orchestrator.fusion.scenario_context(now)) else None),
                "environment_input": ({**environment.status(), "status": "demo_override"}
                                      if context and context.environment else environment.status()),
                "can_input": vehicle_input.status() if vehicle_input is not None else {"source": "pi5.can_ecu", "mode": "dds"},
                "vehicle_ttl_ms": vehicle_ttl_ms,
                "rule_risk": {"candidates": [vars_candidate(c) for c in orchestrator.risk_engine.assessment.candidates],
                              "unavailable_inputs": orchestrator.risk_engine.assessment.unavailable_rules},
                "audio_input": {"device": "C922 USB", "placement": "bench",
                                "horn_enabled": True, "alerts_enabled": orchestrator.risk_engine.policy.audio_alerts_enabled},
                "excluded_sources": ["ins_acceleration"],
                "risk_assessment": json.loads(to_json(assessment)) if assessment else None,
                "driver_attention": orchestrator.risk_engine.driver_attention.status(),
                "vehicle_dynamics": orchestrator.risk_engine.speed_acceleration.status(),
                "front_camera_enabled": bool(args.front_camera_stream),
                "camera_preview": {"stream_url": args.front_camera_stream,
                                   "cabin_stream_url": args.cabin_camera_stream,
                                   "perception_enabled": True},
                "nodes": orchestrator.health_monitor.statuses(now_ns=now),
                "snapshot": json.loads(to_json(snapshot)),
                "command": json.loads(to_json(display_command)) if display_command else None,
                "events": list(recent_events),
                "received_samples": dict(orchestrator.received_samples),
                "rejected_samples": result.rejected_samples,
                "observed_driver_samples": observed,
                "receive_age_ms": list(receive_age_ms),
                "capture_to_state_ms": list(dispatch_age_ms),
                "clock_skew_suspected": any(value < -5 for value in list(receive_age_ms)[-30:]),
                "speech": speech.snapshot(),
                "risk_model": model_state,
                "test_mode": "real-vehicle" if args.real_vehicle_test else "carla-screen" if args.screen_recapture else "carla",
                "vehicle_input": vehicle_input.status() if vehicle_input is not None else None,
            })
            if latency.enabled():
                inputs = [latency.header_key(e.header) for e in snapshot.external]
                if snapshot.driver: inputs.append(latency.header_key(snapshot.driver.header))
                latency.record('state_publish', {'inputs':inputs, 'snapshot_time_ns':now,
                               'event_id':result.hmi.event_id if result.hmi else None},start_ns=now)
                if trace is not None:
                    latency.link(inputs=inputs, snapshot=latency.header_key(snapshot.header), snapshot_time_ns=now,
                                 event_id=result.hmi.event_id if result.hmi else None)
                    latency.finish(trace)
            if time.monotonic() >= next_log:
                LOG.info("live driver=%s received=%s rejected=%d stale=%s", driver is not None,
                         orchestrator.received_samples, result.rejected_samples, snapshot.stale_sources)
                next_log = time.monotonic() + 1
            if time.monotonic() >= next_health:
                health_sequence += 1
                publish_model(bus, "node_health", NodeHealth(
                    Header.now("pi5.hmi", health_sequence), "pi5.hmi",
                    NodeState.READY if driver else NodeState.DEGRADED,
                    time.monotonic() - started, detail="cabin_live" if driver else "driver_missing",
                ))
                next_health = time.monotonic() + 0.5
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            next_tick = max(next_tick + 0.02, time.monotonic())
            stop.wait(max(0, next_tick - time.monotonic()))
    finally:
        environment.close()
        if vehicle_input is not None:
            vehicle_input.close()
        server.shutdown()
        server.server_close()
        bus.close()
        if shadow is not None:
            shadow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
