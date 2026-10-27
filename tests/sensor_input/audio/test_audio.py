from __future__ import annotations

import io
import json
import tarfile
import time
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
import yaml

from codrive.sensor_input.audio.archive import NestedZipArchive, SplitPartTarReader, repair_zip_name
from codrive.sensor_input.audio.features import (
    LogMelConfig,
    decode_wav,
    float32_to_pcm16le,
    log_mel_spectrogram,
)
from codrive.sensor_input.audio.manifest import build_manifest, summarize
from codrive.sensor_input.audio.microphone import ArecordPcmCapture, build_arecord_command
from codrive.sensor_input.audio.rviz import RosAudioVisualizer, waveform_envelope, waveform_levels
from codrive.sensor_input.audio.stream import (
    Stm32AudioInferenceAdapter,
    encode_waveform_as_stm32_frames,
)
from codrive.sensor_input.audio.worker import RollingAudioWorker, TemporalEventFilter
from codrive.contracts.models import AudioKind
from codrive.apps.jetson_publisher import JetsonPerceptionPublisher
from codrive.apps.raspberry_pi.runtime import PiOrchestrator
from codrive.transport import InMemoryBus


def _wav_bytes(*, sample_rate: int = 44_100, duration_s: float = 1.2) -> bytes:
    times = np.arange(round(sample_rate * duration_s), dtype=np.float32) / sample_rate
    samples = 0.4 * np.sin(2.0 * np.pi * 880.0 * times)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(float32_to_pcm16le(samples))
    return payload.getvalue()


def _nested_tar(path: Path, files: dict[str, bytes], *, split_at: int | None = None) -> None:
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    data = nested.getvalue()
    boundaries = (0, split_at, len(data)) if split_at else (0, len(data))
    with tarfile.open(path, "w") as outer:
        for start, end in zip(boundaries, boundaries[1:]):
            info = tarfile.TarInfo(f"dataset.zip.part{start}")
            info.size = end - start
            outer.addfile(info, io.BytesIO(data[start:end]))


def test_split_tar_zip_is_seekable_and_recovers_names(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar"
    _nested_tar(
        archive_path,
        {"교통소음/차량사이렌/sample.wav": _wav_bytes()},
        split_at=137,
    )

    reader = SplitPartTarReader(archive_path)
    assert reader.part_count == 2
    reader.seek(-22, io.SEEK_END)
    assert len(reader.read()) == 22
    reader.close()

    with NestedZipArchive(archive_path) as archive:
        member = next(archive.file_names(suffix=".wav"))
        assert decode_wav(archive.read(member)).size == 19_200

    mojibake = "1.└┌╡┐┬≈/2.┬≈╖«╗τ└╠╖╗"
    assert repair_zip_name(mojibake) == "1.자동차/2.차량사이렌"


def _label(label_name: str, category_03: str) -> bytes:
    payload = {
        "audio": {"fileName": label_name.replace("_1.wav", ".wav")},
        "environment": {"urban": "서울", "direction": "우", "distance": "10m"},
        "annotations": [
            {
                "labelName": label_name,
                "area": {"start": 0.0, "end": 1.0},
                "categories": {
                    "category_01": "교통소음",
                    "category_02": "자동차",
                    "category_03": category_03,
                },
                "subCategory": category_03,
                "decibel": 70,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_manifest_matches_json_to_wav_and_reserves_official_validation(tmp_path: Path) -> None:
    for prefix, name, category in (
        ("T", "train_1.wav", "차량사이렌"),
        ("V", "valid_1.wav", "차량경적"),
    ):
        _nested_tar(
            tmp_path / f"{prefix}L_1.교통소음.tar",
            {f"labels/{name}.json": _label(name, category)},
        )
        _nested_tar(
            tmp_path / f"{prefix}S_1.교통소음.tar",
            {f"자동차/{category}/{name}": _wav_bytes()},
        )

    records = build_manifest(tmp_path)
    assert len(records) == 2
    assert {record.label for record in records} == {"siren", "horn"}
    official = next(record for record in records if record.split == "official_validation")
    assert official.partition == "final_test"
    assert summarize(records)["total"] == 2


def test_log_mel_has_deployment_shape_and_is_finite() -> None:
    waveform = decode_wav(_wav_bytes())
    feature = log_mel_spectrogram(waveform, LogMelConfig())
    assert feature.shape == (1, 64, 96)
    assert feature.dtype == np.float32
    assert np.isfinite(feature).all()
    assert float(feature.std()) == pytest.approx(1.0, rel=1e-5)


def test_waveform_envelope_preserves_min_max_and_reports_levels() -> None:
    waveform = np.tile(np.asarray((-0.75, 0.5), dtype=np.float32), 512)
    x, envelope = waveform_envelope(waveform, bucket_count=64)
    rms, peak, dbfs = waveform_levels(waveform)

    assert x.shape == envelope.shape == (128,)
    assert np.all(np.diff(x) >= 0.0)
    assert float(envelope.min()) == pytest.approx(-0.75)
    assert float(envelope.max()) == pytest.approx(0.5)
    assert rms > 0.0
    assert peak == pytest.approx(0.75)
    assert dbfs < 0.0


def test_laptop_microphone_uses_mono_pcm16_at_model_sample_rate() -> None:
    command = build_arecord_command("pulse")
    assert command == (
        "arecord",
        "-q",
        "-D",
        "pulse",
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-c",
        "1",
        "-r",
        "16000",
    )
    capture = ArecordPcmCapture("pulse", chunk_ms=20)
    assert capture.chunk_bytes == 640


def test_temporal_filter_requires_confirmation_and_hysteresis() -> None:
    event_filter = TemporalEventFilter(
        ema_alpha=1.0,
        on_threshold=0.7,
        off_threshold=0.4,
        on_frames=2,
        off_frames=3,
    )
    assert event_filter.update((0.9, 0.1))[0] is AudioKind.NONE
    assert event_filter.update((0.9, 0.1))[0] is AudioKind.SIREN
    assert event_filter.update((0.2, 0.1))[0] is AudioKind.SIREN
    assert event_filter.update((0.2, 0.1))[0] is AudioKind.SIREN
    assert event_filter.update((0.2, 0.1))[0] is AudioKind.NONE


def test_temporal_filter_uses_independent_class_thresholds() -> None:
    event_filter = TemporalEventFilter(
        ema_alpha=1.0,
        siren_on_threshold=0.9,
        horn_on_threshold=0.6,
        off_threshold=0.4,
        on_frames=1,
    )

    assert event_filter.update((0.7, 0.7))[0] is AudioKind.HORN


def test_temporal_filter_can_observe_horn_without_activating_it() -> None:
    event_filter = TemporalEventFilter(
        ema_alpha=1.0,
        on_frames=1,
        enable_horn=False,
    )

    kind, confidence = event_filter.update((0.1, 0.99))
    assert kind is AudioKind.NONE
    assert confidence == pytest.approx(0.9)
    assert event_filter.smoothed_probabilities == pytest.approx((0.1, 0.99))


def test_rviz_dashboard_status_copy_and_top_down_layout() -> None:
    assert (
        RosAudioVisualizer._card_status(
            AudioKind.NONE, AudioKind.SIREN, 0.92, 0.89, True
        )
        == "VERIFYING"
    )
    assert (
        RosAudioVisualizer._card_status(
            AudioKind.SIREN, AudioKind.SIREN, 0.97, 0.89, True
        )
        == "DETECTED"
    )
    assert (
        RosAudioVisualizer._card_status(
            AudioKind.NONE, AudioKind.HORN, 0.99, 0.63, False
        )
        == "DISABLED / MONITOR ONLY"
    )

    config_path = Path(__file__).parents[3] / "config/rviz/audio_demo.rviz"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    current_view = config["Visualization Manager"]["Views"]["Current"]
    assert current_view["Class"] == "rviz_default_plugins/TopDownOrtho"
    assert config["Panels"] == []


def test_rolling_worker_emits_typed_events_at_100_ms_hop() -> None:
    class FakeBackend:
        model_version = "test-audio-v1"

        def predict_probabilities(self, feature: np.ndarray) -> tuple[float, float]:
            assert feature.shape == (1, 64, 96)
            return 0.91, 0.04

    worker = RollingAudioWorker(
        FakeBackend(),
        event_filter=TemporalEventFilter(ema_alpha=1.0, on_frames=1),
    )
    waveform = np.zeros(16_000 + 2_560, dtype=np.float32)
    results = worker.feed_float(waveform, capture_time_ns=2_000_000_000_000)

    assert len(results) == 3  # at 0.96, 1.06, and 1.16 seconds
    assert all(result.event.kind is AudioKind.SIREN for result in results)
    assert [result.event.header.sequence for result in results] == [1, 2, 3]
    assert results[-1].event.model_version == "test-audio-v1"
    assert results[-1].waveform.shape == (15_360,)
    assert np.isfinite(results[-1].waveform).all()


def test_wav_emulator_uses_real_stm32_decoder_path() -> None:
    class FakeBackend:
        model_version = "serial-test-v1"

        def predict_probabilities(self, feature: np.ndarray) -> tuple[float, float]:
            return 0.03, 0.95

    worker = RollingAudioWorker(
        FakeBackend(),
        event_filter=TemporalEventFilter(ema_alpha=1.0, on_frames=1),
    )
    adapter = Stm32AudioInferenceAdapter(worker)
    serial_frames = encode_waveform_as_stm32_frames(np.zeros(17_000, dtype=np.float32))
    results = []
    for frame in serial_frames:
        # Deliberately fragment serial reads across arbitrary boundaries.
        results.extend(adapter.feed_serial(frame[:13], received_time_ns=3_000_000_000_000))
        results.extend(adapter.feed_serial(frame[13:], received_time_ns=3_000_000_000_000))

    assert results
    assert results[-1].event.kind is AudioKind.HORN
    assert adapter.stats.decoded_frames == len(serial_frames)
    assert adapter.stats.sequence_gaps == 0
    assert adapter.stats.crc_errors == 0


def test_audio_worker_to_dds_fusion_risk_and_hmi() -> None:
    class FakeBackend:
        model_version = "integration-v1"

        def predict_probabilities(self, feature: np.ndarray) -> tuple[float, float]:
            return 0.96, 0.02

    now_ns = time.time_ns()
    worker = RollingAudioWorker(
        FakeBackend(),
        event_filter=TemporalEventFilter(ema_alpha=1.0, on_frames=1),
    )
    result = worker.feed_float(np.zeros(16_000, dtype=np.float32), capture_time_ns=now_ns)[-1]
    bus = InMemoryBus(clock_ns=lambda: now_ns)
    JetsonPerceptionPublisher(bus, "jetson-cabin").audio(result.event)

    outcome = PiOrchestrator(bus).step(now_ns=now_ns)

    assert outcome.risk is not None
    assert outcome.risk.code == "emergency_vehicle"
    assert outcome.hmi is not None
    assert outcome.hmi.display_text == "긴급차량 양보"
