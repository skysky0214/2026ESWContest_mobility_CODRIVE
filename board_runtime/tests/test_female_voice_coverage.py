"""Every active HMI phrase must have prepared F1 audio, including extensions."""
from pathlib import Path
from types import SimpleNamespace
import time

from codrive.apps.pi import LocalSpeech
from codrive.models import Header, HmiCommand, Severity
from codrive.speech import SpeechCache, required_phrases


CACHE = Path(__file__).resolve().parents[2] / 'artifacts/tts/supertonic-ko-f1'


def test_all_active_guidance_has_verified_f1_audio():
    cache = SpeechCache(CACHE)
    cache.load()
    assert cache.manifest['engine'] == 'supertonic-3'
    assert cache.manifest['voice'] == 'F1'
    assert cache.manifest['speaker_id'] == 0
    for text in required_phrases(include_extensions=True):
        assert cache.wave_for(text).startswith(b'RIFF')


def test_active_guidance_never_uses_espeak_fallback(monkeypatch):
    speech = LocalSpeech(False, cache_dir=CACHE)
    phrases = required_phrases(include_extensions=True)
    commands = [HmiCommand(
        header=Header.now('voice-test', i), event_id=f'voice-{i}', risk_code='voice_test',
        severity=Severity.INFO, display_text='음성 테스트', tts_text=text, icon='warning',
        display_ms=3000, source='template', expires_at_ns=time.time_ns()+10_000_000_000,
    ) for i, text in enumerate(phrases)]
    pending = iter([*commands, None])
    speech.pending = SimpleNamespace(get=lambda: next(pending))
    played = []

    def run(args, **kwargs):
        assert args[0] in ('pactl', 'paplay'), 'Known guidance must not invoke a fallback synthesizer'
        if args[0] == 'paplay':
            played.append(kwargs['input'])
        return SimpleNamespace(stdout='test-hdmi\n' if args[0] == 'pactl' else b'')

    monkeypatch.setattr('codrive.apps.pi.subprocess.run', run)
    class Player:
        returncode=0
        def __init__(self,args,**kwargs):self.args=args
        def communicate(self,input=None,timeout=None):
            run(self.args,input=input)
            return b'',b''
    monkeypatch.setattr('codrive.apps.pi.subprocess.Popen',Player)
    speech._run()
    assert speech.snapshot()['completed'] == len(phrases)
    assert speech.snapshot()['last_engine'] == 'supertonic-3'
    assert played == [speech.cache.wave_for(text) for text in phrases]
