from dataclasses import replace
from codrive.models import Header, ScenarioContext, EnvironmentState, Weather, from_json, to_json
from codrive.fusion import SituationFusion
from codrive.runtime import PiOrchestrator, publish_model
from codrive.transport import InMemoryBus
from codrive.risk import RiskEngine, RiskPolicy

NOW = 3_000_000_000_000

def header(source, sequence=1, stamp=NOW):
    return Header.now(source, sequence, capture_time_ns=stamp)

def test_demo_weather_overrides_api_only_while_heartbeat_is_fresh():
    f = SituationFusion()
    real = EnvironmentState(header('pi5.environment'), weather=Weather.CLEAR, api_valid=True)
    f.ingest(real, received_time_ns=NOW)
    demo = EnvironmentState(header('laptop.carla.scenario'), weather=Weather.RAIN,
                            is_night=True, api_valid=True, daylight_valid=True)
    context = ScenarioContext(demo.header, 'rain-night-slow-vehicle', demo)
    assert from_json(ScenarioContext, to_json(context)) == context
    assert f.ingest(context, received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW).environment == demo
    assert f.snapshot(now_ns=NOW+2_000_000_001).environment == real
    # A new API packet does not overwrite an active explicit preset.
    f.ingest(replace(real, header=header('pi5.environment',2)), received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW).environment == demo
    f.ingest(ScenarioContext(header('laptop.carla.scenario',2), ''),received_time_ns=NOW)
    assert f.snapshot(now_ns=NOW).environment.weather == Weather.CLEAR
    assert not f.ingest(ScenarioContext(header('other'), 'emergency-yellow'),received_time_ns=NOW)


def test_emergency_preset_enables_real_audio_rules_and_expires_without_inventing_audio():
    bus=InMemoryBus(clock_ns=lambda:NOW)
    o=PiOrchestrator(bus,risk_engine=RiskEngine(policy=RiskPolicy(audio_alerts_enabled=False)))
    publish_model(bus,'scenario_context',ScenarioContext(header('laptop.carla.scenario'),'emergency-yellow',audio_alerts=True))
    result=o.step(now_ns=NOW)
    assert o.risk_engine.policy.audio_alerts_enabled
    assert o.risk_engine.rules.policy.audio_alerts_enabled
    assert result.snapshot.audio is None  # Phone / microphone detection is required.
    o.step(now_ns=NOW+2_000_000_001)
    assert not o.risk_engine.policy.audio_alerts_enabled
