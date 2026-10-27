from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from codrive.apps.audio import run_audio
from codrive.apps.local_bus import LocalBus
from codrive.models import AudioEvent, AudioKind, Header, NodeHealth
from codrive.health import HealthPolicy, NodeHealthMonitor
from codrive.runtime import decode_model
from codrive.hardware.audio_input import PcmChunk, SerialAudio, wav_chunks
from codrive.hardware.stm32_audio import AudioFrame, AudioStreamDecoder, encode_audio_frame
from codrive.sensor_input.audio.classifier import AudioWindow, RollingAudio, WaveformClassifier

ORIGIN = 1_790_000_000_000_000_000


def test_overlapping_windows_keep_exact_sample_times():
    rolling = RollingAudio(40, 20)
    chunks = [np.full(320, i, dtype="<i2").tobytes() for i in range(3)]
    results = []
    for i, data in enumerate(chunks):
        results.extend(rolling.feed(data, ORIGIN + (i + 1) * 20_000_000))
    assert len(results) == 2
    assert results[0].pcm16 == chunks[0] + chunks[1]
    assert results[1].pcm16 == chunks[1] + chunks[2]
    assert results[1].capture_time_ns == ORIGIN + 60_000_000
    assert len(rolling.buffer) < rolling.size


def test_gap_discards_old_pcm():
    rolling = RollingAudio(40, 20)
    assert not rolling.feed(bytes(640), ORIGIN + 20_000_000)
    assert not rolling.feed(bytes(640), ORIGIN + 80_000_000)
    windows = rolling.feed(bytes(640), ORIGIN + 100_000_000)
    assert len(windows) == 1
    rolling.reset()
    with pytest.raises(ValueError):
        rolling.feed(b"x", ORIGIN)


def test_serial_crc_sequence_gap_restart_and_duplicate():
    receiver = SerialAudio("/injected")
    pcm = bytes(640)
    decoder = AudioStreamDecoder()
    corrupted = bytearray(encode_audio_frame(pcm, sequence=0, timestamp_us=0))
    corrupted[-1] ^= 1
    valid = encode_audio_frame(pcm, sequence=1, timestamp_us=20000)
    frames = decoder.feed(bytes(corrupted) + valid)
    assert decoder.crc_errors == 1 and len(frames) == 1
    first = receiver.accept(frames[0], ORIGIN)
    assert first.end_ns == ORIGIN
    assert receiver.accept(frames[0], ORIGIN + 1) is None
    assert receiver.duplicates == 1
    gap = receiver.accept(AudioFrame(3, 60000, 16000, 1, pcm), ORIGIN + 40_000_000)
    assert gap.discontinuity and receiver.sequence_gaps == 1
    restart = receiver.accept(AudioFrame(0, 0, 16000, 1, pcm), ORIGIN + 60_000_000)
    assert restart.discontinuity and restart.end_ns == ORIGIN + 60_000_000
    assert receiver.restarts == 1


def write_wav(path, rate=16000):
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(bytes(1920))


def test_wav_format_eof_and_capture_only_never_warns(tmp_path, capsys):
    path = tmp_path / "sample.wav"
    write_wav(path)
    bus = LocalBus()
    assert run_audio(wav_chunks(path), bus=bus, window_ms=40, hop_ms=20, print_json=True) == 2
    assert not bus.take_json("audio_event")
    diagnostics = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(item["mode"] == "capture_only" and item["rms"] == 0 for item in diagnostics)
    write_wav(path, 48000)
    with pytest.raises(ValueError, match="16kHz"):
        list(wav_chunks(path))


def test_input_failure_closes_source_and_marks_failed():
    closed = []

    def chunks():
        try:
            yield PcmChunk(bytes(640), ORIGIN)
            raise TimeoutError("test silence")
        finally:
            closed.append(True)

    bus = LocalBus()
    with pytest.raises(TimeoutError):
        run_audio(chunks(), bus=bus)
    assert closed
    states = [json.loads(e.payload_json)["state"] for e in bus.take_json("node_health", 16)]
    assert "failed" in states


def test_audio_event_reaches_risk_and_bounded_exit_closes_source(capsys):
    closed = []

    def chunks():
        try:
            for i in range(10):
                yield PcmChunk(bytes(640), ORIGIN + i * 20_000_000)
        finally:
            closed.append(True)

    class Siren:
        def classify(self, window, header):
            return AudioEvent(header, AudioKind.SIREN, 0.99, window.window_ms, "injected")

    bus = LocalBus()
    assert (
        run_audio(
            chunks(),
            bus=bus,
            classifier=Siren(),
            window_ms=40,
            hop_ms=20,
            max_windows=1,
            local_risk=True,
            replay=True,
        )
        == 1
    )
    assert closed and len(bus.take_json("audio_event", 16)) == 1
    monitor = NodeHealthMonitor(policy=HealthPolicy(expected_nodes=("jetson-cabin.audio",)))
    for envelope in bus.take_json("node_health", 16):
        assert monitor.ingest(decode_model(envelope, NodeHealth))
    assert monitor.missing_nodes() == ()
    assessment = json.loads(capsys.readouterr().out)
    assert assessment["code"] == "emergency_vehicle"


@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
def test_torchscript_waveform_contract_and_invalid_outputs(tmp_path, device):
    torch = pytest.importorskip("torch")

    if device.startswith("cuda") and not torch.cuda.is_available():
        pytest.skip("Requires CUDA")

    class Scores(torch.nn.Module):
        def forward(self, x):
            return torch.stack(
                (x.mean(dim=1), x.mean(dim=1) + 10, x.mean(dim=1), x.mean(dim=1)), dim=1
            )

    path = tmp_path / "audio.pt"
    torch.jit.trace(Scores(), torch.zeros(1, 640)).save(str(path))
    backend = WaveformClassifier(path, device=device, window_ms=40)
    window = AudioWindow(bytes(1280), ORIGIN, 40)
    event = backend.classify(window, Header.now("jetson-cabin.audio", 1, capture_time_ns=ORIGIN))
    assert event.kind == AudioKind.SIREN and event.confidence > 0.99
    bad = WaveformClassifier(path, device=device, window_ms=40, output="probabilities")
    with pytest.raises(ValueError, match="probabilities"):
        bad.classify(window, event.header)
    with pytest.raises(ValueError, match="length"):
        backend.classify(AudioWindow(bytes(640), ORIGIN, 20), event.header)
    with pytest.raises(FileNotFoundError):
        WaveformClassifier(tmp_path / "absent.pt")


@pytest.mark.parametrize("window,hop", [(0, 100), (960, 0), (100, 200), (True, 20)])
def test_invalid_window_configuration(window, hop):
    with pytest.raises(ValueError):
        RollingAudio(window, hop)


def test_tensorrt_engine_runs_on_current_jetson(tmp_path):
    trt = pytest.importorskip("tensorrt")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("Requires CUDA")
    logger = trt.Logger(trt.Logger.ERROR)
    builder = trt.Builder(logger)
    network = builder.create_network(0)
    waveform = network.add_input("waveform", trt.float32, (1, 640))
    mean = network.add_reduce(waveform, trt.ReduceOperation.AVG, 2, True).get_output(0)
    joined = network.add_concatenation([mean, mean, mean, mean])
    joined.axis = 1
    joined.get_output(0).name = "scores"
    network.mark_output(joined.get_output(0))
    plan = builder.build_serialized_network(network, builder.create_builder_config())
    assert plan is not None
    path = tmp_path / "test.engine"
    path.write_bytes(bytes(plan))
    classifier = WaveformClassifier(path, backend="tensorrt", window_ms=40)
    event = classifier.classify(
        AudioWindow(bytes(1280), ORIGIN, 40),
        Header.now("jetson-cabin.audio", 1, capture_time_ns=ORIGIN),
    )
    assert event.kind == AudioKind.NONE and event.confidence == pytest.approx(0.25)


def test_yyj_temporal_filter_keeps_independent_thresholds_and_resets():
    from codrive.sensor_input.audio.temporal import TemporalEventFilter

    filter_ = TemporalEventFilter(
        ema_alpha=1, siren_on_threshold=0.89, horn_on_threshold=0.63, on_frames=2, off_frames=2
    )
    assert filter_.update((0.1, 0.8))[0] is AudioKind.NONE
    assert filter_.update((0.1, 0.8))[0] is AudioKind.HORN
    filter_.reset()
    assert filter_.update((0.1, 0.8))[0] is AudioKind.NONE
    only = TemporalEventFilter(ema_alpha=1, on_frames=1, enable_horn=False)
    assert only.update((0.1, 0.99))[0] is AudioKind.NONE
    assert only.smoothed_probabilities[1] == pytest.approx(0.99)


def test_real_yyj_checkpoint_runs_and_uses_training_preprocessing():
    from codrive.sensor_input.audio.features import log_mel_spectrogram
    from codrive.sensor_input.audio.logmel import LogMelClassifier

    path = Path(__file__).resolve().parents[2] / "artifacts/audio/initial_model/audio_event_best.pt"
    if not path.is_file():
        pytest.skip("Fetch yyj model asset for actual-checkpoint test")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("Requires CUDA")
    classifier = LogMelClassifier(path, device="cuda:0")
    assert classifier.event_filter.siren_on_threshold == pytest.approx(0.89)
    assert classifier.event_filter.horn_on_threshold == pytest.approx(0.63)
    feature = log_mel_spectrogram(np.zeros(15360, dtype=np.float32), classifier.config)
    assert feature.shape == (1, 64, 96) and np.isfinite(feature).all()
    event = classifier.classify(
        AudioWindow(bytes(30720), ORIGIN, 960),
        Header.now("jetson-cabin.audio", 1, capture_time_ns=ORIGIN),
    )
    assert event.model_version == "audio-event-v1" and 0 <= event.confidence <= 1
    classifier.reset()
    assert classifier.event_filter.smoothed_probabilities == (0.0, 0.0)
