"""Offline Korean female speech: prepare neural WAVs once, play cached PCM at runtime."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import time
import wave
from pathlib import Path

from .guidance import TEMPLATES, EXTRA_TEMPLATES

DEFAULT_CACHE_DIR = Path("artifacts/tts/supertonic-ko-f1")
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    "sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2"
)
TEST_TEXT = "코드라이브 여성 음성 안내입니다. 전방을 주시하세요."
FALLBACK_TEXT = "주변 위험 요소를 확인하세요."


def phrase_name(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() + ".wav"


def required_phrases(*, include_extensions=False) -> list[str]:
    templates = {**TEMPLATES, **EXTRA_TEMPLATES} if include_extensions else TEMPLATES
    return sorted({template.speech for template in templates.values()} | {TEST_TEXT, FALLBACK_TEXT})


class SpeechCache:
    def __init__(self, directory: str | Path = DEFAULT_CACHE_DIR) -> None:
        self.directory = Path(directory)
        self.manifest: dict = {}
        self._waves: dict[str, bytes] = {}

    def load(self) -> None:
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("language") != "ko"
            or manifest.get("voice") != "F1"
            or manifest.get("engine") != "supertonic-3"
        ):
            raise ValueError("Expected Supertonic 3 Korean female F1 speech cache")
        waves = {}
        entries = manifest.get("clips")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
            raise ValueError("Speech cache must contain 1 to 64 clips")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("text"), str)
                or not entry["text"]
            ):
                raise ValueError("Invalid speech cache entry")
            text = entry["text"]
            if entry.get("file") != phrase_name(text) or text in waves:
                raise ValueError("Invalid or duplicate speech cache entry")
            data = (self.directory / entry["file"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                raise ValueError("Speech clip checksum mismatch")
            try:
                with wave.open(io.BytesIO(data), "rb") as source:
                    if (
                        source.getnchannels() != 1
                        or source.getsampwidth() != 2
                        or source.getframerate() not in (24000, 44100)
                        or not 0 < source.getnframes() <= 15 * source.getframerate()
                    ):
                        raise ValueError(
                            "Speech clips must be nonempty mono PCM16 at 24/44.1 kHz, <=15s"
                        )
                    if len(source.readframes(source.getnframes())) != source.getnframes() * 2:
                        raise ValueError("Truncated speech clip")
            except (wave.Error, EOFError) as exc:
                raise ValueError("Invalid cached WAV") from exc
            waves[text] = data
        if not waves:
            raise ValueError("Speech cache is empty")
        self.manifest, self._waves = manifest, waves

    def wave_for(self, text: str) -> bytes:
        if not self._waves:
            self.load()
        try:
            return self._waves[text]
        except KeyError as exc:
            raise ValueError(
                "No prepared neural speech for this phrase; rebuild the cache"
            ) from exc

    def verify_templates(self) -> None:
        self.load()
        for text in required_phrases():
            self.wave_for(text)


def physical_sink() -> str:
    sink = subprocess.run(
        ["pactl", "get-default-sink"],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    ).stdout.strip()
    if not sink or sink == "auto_null":
        raise RuntimeError("No physical audio output is available (dummy sink)")
    return sink


def build_cache(model_dir: Path, output: Path, *, steps: int = 8, speed: float = 1.0) -> dict:
    # Neural inference is a preparation step, never part of the Pi's dispatch loop.
    import numpy as np
    import sherpa_onnx

    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty output directory; existing voice caches are not overwritten")
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "duration_predictor": "duration_predictor.int8.onnx",
        "text_encoder": "text_encoder.int8.onnx",
        "vector_estimator": "vector_estimator.int8.onnx",
        "vocoder": "vocoder.int8.onnx",
        "tts_json": "tts.json",
        "unicode_indexer": "unicode_indexer.bin",
        "voice_style": "voice.bin",
    }
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            supertonic=sherpa_onnx.OfflineTtsSupertonicModelConfig(
                **{key: str(model_dir / name) for key, name in files.items()},
            ),
            num_threads=2,
            provider="cpu",
        )
    )
    tts = sherpa_onnx.OfflineTts(config)
    generation = sherpa_onnx.GenerationConfig()
    generation.sid = 0  # Upstream voice.bin sorts F1..F5, then M1..M5.
    generation.num_steps = steps
    generation.speed = speed
    generation.extra = {"lang": "ko"}
    manifest = {
        "schema_version": 1,
        "engine": "supertonic-3",
        "voice": "F1",
        "speaker_id": 0,
        "language": "ko",
        "sample_rate": int(tts.sample_rate),
        "model_url": MODEL_URL,
        "sherpa_onnx_version": sherpa_onnx.__version__,
        "steps": steps,
        "speed": speed,
        "model_sha256": {
            name: hashlib.sha256((model_dir / name).read_bytes()).hexdigest()
            for name in files.values()
        },
        "clips": [],
    }
    for text in required_phrases(include_extensions=True):
        started = time.perf_counter()
        audio = tts.generate(text, generation)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if (
            audio.sample_rate not in (24000, 44100)
            or not np.isfinite(samples).all()
            or not samples.size
        ):
            raise ValueError("Invalid neural speech output")
        if np.max(np.abs(samples)) < 0.001:
            raise ValueError("Neural speech output is silent")
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        path = output / phrase_name(text)
        with wave.open(str(path), "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(audio.sample_rate)
            destination.writeframes(pcm.tobytes())
        entry = {
            "text": text,
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "duration_s": samples.size / audio.sample_rate,
            "generation_s": time.perf_counter() - started,
        }
        manifest["clips"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    SpeechCache(output).verify_templates()
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--model-dir", type=Path, required=True)
    build.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    build.add_argument("--steps", type=int, default=8)
    build.add_argument("--speed", type=float, default=1.0)
    for action in ("verify", "play-test"):
        subparsers.add_parser(action).add_argument(
            "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR
        )
    args = parser.parse_args(argv)
    if args.action == "build":
        build_cache(args.model_dir, args.cache_dir, steps=args.steps, speed=args.speed)
    else:
        cache = SpeechCache(args.cache_dir)
        cache.verify_templates()
        if args.action == "play-test":
            sink = physical_sink()
            subprocess.run(
                ["paplay", "--client-name=CODRIVE", "--device=" + sink],
                input=cache.wave_for(TEST_TEXT),
                capture_output=True,
                timeout=20,
                check=True,
            )
            print(json.dumps({"played": True, "voice": "F1", "sink": sink}, ensure_ascii=False))
        else:
            print(
                json.dumps({"verified": True, "voice": "F1", "clips": len(cache.manifest["clips"])})
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
