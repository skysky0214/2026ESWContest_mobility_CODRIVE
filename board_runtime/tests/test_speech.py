"""Neural clip integrity, expiry and local output checks without an audio device."""

from __future__ import annotations

import hashlib
import io
import json
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codrive.apps.pi import LocalSpeech
from codrive.guidance import HmiPolicy
from codrive.models import Header, RiskEvent, Severity
from codrive.speech import SpeechCache, phrase_name, required_phrases


def test_bundled_neural_voice_contains_all_guidance_phrases():
    root = Path(__file__).resolve().parents[2]
    cache = SpeechCache(root / "artifacts/tts/supertonic-ko-f1")
    cache.verify_templates()
    assert cache.manifest["voice"] == "F1"
    assert cache.manifest["language"] == "ko"
    for text in required_phrases():
        with wave.open(io.BytesIO(cache.wave_for(text)), "rb") as audio:
            assert audio.getframerate() == cache.manifest["sample_rate"]
            assert audio.getnframes() / audio.getframerate() < 15


def prepare_cache(folder):
    folder.mkdir()
    clips = []
    for text in required_phrases():
        content = io.BytesIO()
        with wave.open(content, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x01\x00" * 240)
        data = content.getvalue()
        (folder / phrase_name(text)).write_bytes(data)
        clips.append(
            {"text": text, "file": phrase_name(text), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = {
        "schema_version": 1,
        "voice": "F1",
        "language": "ko",
        "engine": "supertonic-3",
        "clips": clips,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def command():
    now = time.time_ns()
    event = RiskEvent(
        Header.now("test", 1),
        "test-event",
        "driver_phone_observation",
        Severity.INFO,
        1,
        "observe",
        "phone_observation",
        (),
        now + 10_000_000_000,
    )
    return HmiPolicy().render(event)


def test_cache_has_all_current_templates_and_never_changes_unknown_text_to_generic(tmp_path):
    folder = tmp_path / "voice"
    prepare_cache(folder)
    cache = SpeechCache(folder)
    cache.verify_templates()
    for text in required_phrases():
        assert cache.wave_for(text).startswith(b"RIFF")
    with pytest.raises(ValueError, match="No prepared"):
        cache.wave_for("unprepared text")


@pytest.mark.parametrize("damage", ["checksum", "path", "voice", "truncated", "empty"])
def test_broken_or_wrong_voice_cache_is_rejected(tmp_path, damage):
    folder = tmp_path / "voice"
    manifest = prepare_cache(folder)
    if damage == "checksum":
        (folder / manifest["clips"][0]["file"]).write_bytes(b"not a wave")
    elif damage == "path":
        manifest["clips"][0]["file"] = "../outside.wav"
    elif damage == "voice":
        manifest["voice"] = "M1"
    elif damage == "truncated":
        clip = manifest["clips"][0]
        path = folder / clip["file"]
        data = path.read_bytes()[:-10]
        path.write_bytes(data)
        clip["sha256"] = hashlib.sha256(data).hexdigest()
    else:
        manifest["clips"] = []
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        SpeechCache(folder).load()


def test_runtime_plays_prepared_neural_wav_without_synthesis(monkeypatch, tmp_path):
    folder = tmp_path / "voice"
    prepare_cache(folder)
    speech = LocalSpeech(False, cache_dir=folder)
    item = command()
    speech.pending.put(item)
    # The real queue is bounded, so replace it with a finite worker input.
    inputs = iter([item, None])
    speech.pending = SimpleNamespace(get=lambda: next(inputs))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "paplay":
            assert speech.snapshot()["playing"] is True
        return SimpleNamespace(stdout="hdmi-test\n" if args[0] == "pactl" else b"")

    monkeypatch.setattr("codrive.apps.pi.subprocess.run", run)
    class Player:
        returncode=0
        def __init__(self,args,**kwargs):self.args=args
        def communicate(self,input=None,timeout=None):
            run(self.args,input=input)
            return b"",b""
    monkeypatch.setattr("codrive.apps.pi.subprocess.Popen",Player)
    speech._run()
    assert [args[0] for args, _ in calls] == ["pactl", "paplay"]
    assert calls[1][1]["input"].startswith(b"RIFF")
    assert "--device=hdmi-test" in calls[1][0]
    assert speech.snapshot()["completed"] == 1
    assert speech.snapshot()["voice"] == "F1"
    assert speech.snapshot()["playing"] is False


def test_expired_queue_entry_is_not_played(monkeypatch, tmp_path):
    speech = LocalSpeech(False, cache_dir=tmp_path)
    old = replace(command(), expires_at_ns=time.time_ns() - 1)
    inputs = iter([old, None])
    speech.pending = SimpleNamespace(get=lambda: next(inputs))

    def unexpected(*args, **kwargs):
        pytest.fail("Expired speech must not access the audio device")

    monkeypatch.setattr("codrive.apps.pi.subprocess.run", unexpected)
    speech._run()
    assert speech.snapshot()["completed"] == 0


def test_expiry_during_output_selection_prevents_playback(monkeypatch, tmp_path):
    folder = tmp_path / "voice"
    prepare_cache(folder)
    speech = LocalSpeech(False, cache_dir=folder)
    item = command()
    inputs = iter([item, None])
    speech.pending = SimpleNamespace(get=lambda: next(inputs))

    def run(args, **kwargs):
        assert args[0] == "pactl"
        monkeypatch.setattr("codrive.apps.pi.time.time_ns", lambda: item.expires_at_ns + 1)
        return SimpleNamespace(stdout="hdmi-test\n")

    monkeypatch.setattr("codrive.apps.pi.subprocess.run", run)
    speech._run()
    assert speech.snapshot()["completed"] == 0


def test_missing_female_phrase_never_falls_back_to_local_synthesis(monkeypatch, tmp_path):
    folder = tmp_path / "voice"
    prepare_cache(folder)
    speech = LocalSpeech(False, cache_dir=folder)
    item = replace(command(), risk_code="emergency_vehicle",
                   display_text="왼쪽 사이렌 감지",
                   tts_text="왼쪽에서 사이렌이 감지되었습니다. 주변을 확인하세요.")
    inputs = iter([item, None])
    speech.pending = SimpleNamespace(get=lambda: next(inputs))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="hdmi-test\n" if args[0] == "pactl" else b"RIFFtest")
    monkeypatch.setattr("codrive.apps.pi.subprocess.run", run)
    speech._run()
    assert [c[0] for c in calls] == ["pactl"]
    assert speech.snapshot()["completed"] == 0
    assert "No prepared neural speech" in speech.snapshot()["error"]


def test_collision_preempts_critical_lane_and_lower_priority_cannot_replace_it(monkeypatch,tmp_path):
    import threading
    speech=LocalSpeech(False,cache_dir=tmp_path);speech.enabled=True
    speech.cache.wave_for=lambda text:b'RIFFtest'
    monkeypatch.setattr('codrive.apps.pi.physical_sink',lambda:'test')
    started=threading.Event();collision_started=threading.Event();release_collision=threading.Event()
    players=[]
    class Player:
        def __init__(self,args,**kwargs):
            self.args=args;self.returncode=0;self.release=threading.Event();self.terminated=False
            players.append(self)
        def communicate(self,input=None,timeout=None):
            if len(players)==1:
                started.set();assert self.release.wait(2)
            else:
                collision_started.set();assert release_collision.wait(2)
            return b'',b''
        def terminate(self):
            self.terminated=True;self.returncode=-15;self.release.set()
    monkeypatch.setattr('codrive.apps.pi.subprocess.Popen',Player)
    lane=replace(command(),event_id='lane',risk_code='lane_departure',severity=Severity.CRITICAL)
    collision=replace(command(),event_id='collision',risk_code='pedestrian_collision',severity=Severity.WARNING)
    speech.pending.put(lane)
    worker=threading.Thread(target=speech._run,daemon=True);worker.start()
    assert started.wait(2)
    speech.submit(collision)
    assert collision_started.wait(2)
    assert players[0].terminated
    assert speech.current_command().event_id=='collision'
    speech.submit(lane)
    assert speech.pending.empty()
    release_collision.set();speech.pending.put(None);worker.join(2)
    assert not worker.is_alive()
    assert speech.snapshot()['interrupted']==1
    assert speech.snapshot()['completed']==1
    assert speech.snapshot()['error'] is None


def test_queued_collision_is_not_replaced_by_lane(tmp_path):
    speech=LocalSpeech(False,cache_dir=tmp_path);speech.enabled=True
    collision=replace(command(),event_id='collision',risk_code='object_collision',severity=Severity.WARNING)
    lane=replace(command(),event_id='lane',risk_code='lane_departure',severity=Severity.CRITICAL)
    speech.submit(collision);speech.submit(lane)
    assert speech.pending.get_nowait().event_id=='collision'
