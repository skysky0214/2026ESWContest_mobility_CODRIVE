from dataclasses import replace
from codrive.risk import RiskEngine
from codrive.models import (
    BoundingBox,
    ExternalPerception,
    Header,
    SituationSnapshot,
    TrackedObject,
    VehicleState,
)


def test_team_fsm_retains_state_when_event_suppressed_and_releases():
    now = 3_000_000_000_000
    h = Header.now("test", 1, capture_time_ns=now)
    obj = TrackedObject(
        1, "person", 0.95, BoundingBox(0.4, 0.2, 0.6, 0.8), distance_m=10, ttc_s=1.2, in_path=True
    )
    frame = ExternalPerception(
        h, "front", "test", objects=(obj,), lane_offset_m=0.9, lane_confidence=0.9
    )
    snapshot = SituationSnapshot(
        h, vehicle=VehicleState(h, 40 / 3.6, 0, False, 0), external=(frame,)
    )
    engine = RiskEngine()
    assert engine.evaluate(snapshot, now_ns=now).code == "pedestrian_collision"
    assert engine.assessment.stable_level.value == "EMERGENCY"
    assert engine.evaluate(snapshot, now_ns=now + 50_000_000) is None
    assert engine.assessment.stable_level.value == "EMERGENCY"
    empty = replace(snapshot, external=())
    engine.evaluate(empty, now_ns=now + 100_000_000)
    assert engine.assessment.held
    engine.evaluate(empty, now_ns=now + 600_000_000)
    assert engine.assessment.stable_level.value == "NORMAL"


def test_restored_collision_guidance_resolves_to_female_audio():
    from codrive.guidance import HmiPolicy
    from codrive.speech import SpeechCache, DEFAULT_CACHE_DIR
    now = 3_000_000_000_000
    h = Header.now("test", 1, capture_time_ns=now)
    obj = TrackedObject(1, "person", .95, BoundingBox(.4, .2, .6, .8),
                        distance_m=10, ttc_s=1.2, in_path=True)
    frame = ExternalPerception(h, "front", "test", objects=(obj,),
                               lane_offset_m=.9, lane_confidence=.9)
    snapshot = SituationSnapshot(h, vehicle=VehicleState(h, 40/3.6, 0, False, 0),
                                 external=(frame,))
    event = RiskEngine().evaluate(snapshot, now_ns=now)
    command = HmiPolicy().render(event)
    assert command.risk_code == "pedestrian_collision"
    assert command.tts_text == "전방 보행자 위험입니다. 즉시 제동하세요."
    cache = SpeechCache(DEFAULT_CACHE_DIR)
    cache.load()
    assert cache.manifest["voice"] == "F1"
    assert cache.wave_for(command.tts_text).startswith(b"RIFF")


def test_latest_model_display_cannot_publish_or_replace_team_warning():
    from codrive.risk_ml.shadow import ShadowRisk
    class Predictor:
        contract_version = 3
        metadata = {}
        def predict(self, sample):
            return {"status": "ok", "xgboost_risk_level": "LOW"}
    now = 3_000_000_000_000
    h = Header.now("test", 1, capture_time_ns=now)
    obj = TrackedObject(1, "person", .95, BoundingBox(.4,.2,.6,.8),
                        distance_m=10, ttc_s=1.2, in_path=True)
    snapshot = SituationSnapshot(h, external=(ExternalPerception(h,"front","test",objects=(obj,)),))
    engine = RiskEngine()
    event = engine.evaluate(snapshot, now_ns=now)
    shadow = ShadowRisk(Predictor())
    try:
        state = shadow.update(snapshot, now_ns=now)
        assert state["feature_contract_version"] == 3
        assert state["mode"] == "display_only"
        assert event.code == "pedestrian_collision"
        assert engine.assessment.stable_level.value == "EMERGENCY"
    finally:
        shadow.close()
