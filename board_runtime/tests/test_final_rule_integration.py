from codrive.apps.local_bus import LocalBus
from codrive.models import (
    AudioEvent,
    AudioKind,
    ExternalPerception,
    Header,
    Severity,
    TrackedObject,
    TrafficLight,
    VehicleState,
    from_json,
)
from codrive.v3_feature_engine import RiskEngine
from codrive.runtime import PiOrchestrator, publish_model

NOW = 1500000000000000000


def test_final_external_contract_and_environment_reach_hmi():
    h = Header.now("jetson-front.front", 1, capture_time_ns=NOW)
    # Main front camera includes in_path even though no risk rule consumes it.
    obj = from_json(
        TrackedObject,
        '{"track_id":1,"class_name":"car","confidence":0.8,"bbox":{"x1":0.2,"y1":0.2,"x2":0.8,"y2":0.9},"in_path":null}',
    )
    bus = LocalBus()
    publish_model(
        bus,
        "external_perception",
        ExternalPerception(h, "front", "test", objects=(obj,), traffic_light=TrafficLight.RED),
    )
    publish_model(
        bus,
        "vehicle_state",
        VehicleState(Header.now("pi5.panda", 1, capture_time_ns=NOW), 3, 0, False, 10),
    )
    from codrive.models import EnvironmentState, Weather

    env = EnvironmentState(
        Header.now("pi5.environment", 1, capture_time_ns=NOW),
        Weather.FOG,
        True,
        visibility_m=100,
        api_valid=True,
        daylight_valid=True,
        weather_time_ns=NOW,
        weather_valid_until_ns=NOW + 4500000000000,
    )
    publish_model(bus, "environment_state", env)
    pi = PiOrchestrator(bus, risk_engine=RiskEngine())
    result = pi.step(now_ns=NOW)
    assert not result.rejected_samples
    assert result.risk.code == "red_light_approach" and result.risk.severity is Severity.CRITICAL
    assert "environment_weight=" in " ".join(result.risk.evidence)
    assert "빨간불" in result.hmi.display_text


def test_directional_horn_is_rendered_and_expiry_drops_it():
    bus = LocalBus()
    audio = AudioEvent(
        Header.now("jetson-cabin.audio", 1, capture_time_ns=NOW),
        AudioKind.HORN,
        0.8,
        960,
        "test",
        "right",
    )
    publish_model(bus, "audio_event", audio)
    pi = PiOrchestrator(bus, risk_engine=RiskEngine())
    result = pi.step(now_ns=NOW)
    assert result.risk.code == "horn_alert" and "오른쪽" in result.hmi.display_text
    assert pi.step(now_ns=NOW + 1100000000).snapshot.audio is None
