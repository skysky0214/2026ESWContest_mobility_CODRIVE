"""Bounded PCM windows and explicit waveform classifier contract.

Models take float32 normalized mono waveform [1, N] at 16 kHz and return
[1, C] logits or probabilities in the configured label order.
"""

from __future__ import annotations

import math
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...models import AudioEvent, AudioKind, Header


@dataclass(frozen=True, slots=True)
class AudioWindow:
    pcm16: bytes
    capture_time_ns: int  # Last sample, not the beginning of a rolling window.
    window_ms: int


class RollingAudio:
    def __init__(self, window_ms=960, hop_ms=100):
        if any(
            isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in (window_ms, hop_ms)
        ):
            raise ValueError("window_ms and hop_ms must be positive integers")
        if hop_ms > window_ms or window_ms > 10000:
            raise ValueError("Require hop_ms <= window_ms <= 10000")
        self.window_ms, self.hop_ms = window_ms, hop_ms
        self.size, self.hop = window_ms * 32, hop_ms * 32
        self.buffer = bytearray()
        self.end_ns = None

    def reset(self):
        self.buffer.clear()
        self.end_ns = None

    def feed(self, pcm16: bytes, end_ns: int, *, discontinuity=False):
        if not pcm16 or len(pcm16) % 2 or len(pcm16) > 65536:
            raise ValueError("PCM chunks must be nonempty, even sized, <=65536 bytes")
        start_ns = end_ns - len(pcm16) // 2 * 62500
        if discontinuity or (self.end_ns is not None and abs(start_ns - self.end_ns) > 2_000_000):
            self.reset()
        self.buffer.extend(pcm16)
        self.end_ns = end_ns
        windows = []
        while len(self.buffer) >= self.size:
            remaining_samples = (len(self.buffer) - self.size) // 2
            windows.append(
                AudioWindow(
                    bytes(self.buffer[: self.size]),
                    end_ns - remaining_samples * 62500,
                    self.window_ms,
                )
            )
            del self.buffer[: self.hop]
        return windows


class WaveformClassifier:
    def __init__(
        self,
        model_path,
        *,
        backend="torchscript",
        device="cuda:0",
        labels=("none", "siren", "horn", "other"),
        output="logits",
        threshold=0.7,
        window_ms=960,
    ):
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        self.labels = tuple(AudioKind(x) for x in labels)
        if len(set(self.labels)) != len(self.labels) or not self.labels:
            raise ValueError("labels must be unique AudioKind names")
        if output not in ("logits", "probabilities"):
            raise ValueError("output must be logits or probabilities")
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"Audio model not found: {path}")
        import torch

        self.torch = torch
        self.device = torch.device(device)
        self.threshold, self.output, self.version = threshold, output, path.name
        self.samples = window_ms * 16
        self.runtime = None
        if backend == "torchscript":
            self.model = torch.jit.load(str(path), map_location=self.device).eval()
        elif backend == "tensorrt":
            if self.device.type != "cuda":
                raise ValueError("TensorRT audio requires CUDA")
            import tensorrt as trt

            self.logger = trt.Logger(trt.Logger.WARNING)
            self.runtime = trt.Runtime(self.logger)
            self.engine = self.runtime.deserialize_cuda_engine(path.read_bytes())
            if self.engine is None:
                raise ValueError("Cannot deserialize TensorRT audio engine on this board")
            self.context = self.engine.create_execution_context()
            inputs = [
                n for n in self.engine if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT
            ]
            outputs = [
                n for n in self.engine if self.engine.get_tensor_mode(n) == trt.TensorIOMode.OUTPUT
            ]
            if len(inputs) != 1 or len(outputs) != 1:
                raise ValueError("Audio engine requires one waveform input and one score output")
            self.input_name, self.output_name = inputs[0], outputs[0]
            for name in inputs + outputs:
                if (
                    self.engine.get_tensor_dtype(name) != trt.float32
                    or self.engine.get_tensor_location(name) != trt.TensorLocation.DEVICE
                ):
                    raise ValueError("Audio engine tensors must be device float32")
            shape = self.engine.get_tensor_shape(self.input_name)
            if tuple(shape) != (1, self.samples) and not self.context.set_input_shape(
                self.input_name, (1, self.samples)
            ):
                raise ValueError("Audio engine waveform shape mismatch")
            if tuple(self.context.get_tensor_shape(self.output_name)) != (1, len(self.labels)):
                raise ValueError("Audio engine output shape mismatch")
        else:
            raise ValueError("backend must be torchscript or tensorrt")

    def classify(self, window: AudioWindow, header: Header) -> AudioEvent:
        torch = self.torch
        samples = np.frombuffer(window.pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if len(samples) != self.samples:
            raise ValueError("Audio model input length mismatch")
        device_context = (
            torch.cuda.device(self.device) if self.device.type == "cuda" else _nullcontext()
        )
        with device_context, torch.inference_mode():
            waveform = torch.from_numpy(samples).reshape(1, -1).to(self.device)
            if self.runtime is None:
                scores = self.model(waveform)
            else:
                scores = torch.empty((1, len(self.labels)), device=self.device, dtype=torch.float32)
                stream = torch.cuda.current_stream(self.device)
                self.context.set_tensor_address(self.input_name, waveform.data_ptr())
                self.context.set_tensor_address(self.output_name, scores.data_ptr())
                if not self.context.execute_async_v3(stream.cuda_stream):
                    raise RuntimeError("TensorRT audio execution failed")
            if not isinstance(scores, torch.Tensor) or tuple(scores.shape) != (
                1,
                len(self.labels),
            ):
                raise ValueError("Audio model must return tensor [1, class_count]")
            scores = scores.float()
            if not torch.isfinite(scores).all().item():
                raise ValueError("Audio model returned non-finite scores")
            if self.output == "logits":
                scores = torch.softmax(scores, dim=-1)
            elif (
                scores.min().item() < 0
                or scores.max().item() > 1
                or not torch.allclose(
                    scores.sum(dim=-1), torch.ones(1, device=self.device), atol=1e-3
                )
            ):
                raise ValueError("Audio probabilities must sum to one and be in [0,1]")
            confidence, index = scores[0].max(dim=0)
            confidence, index = float(confidence.item()), int(index.item())
        kind = self.labels[index] if confidence >= self.threshold else AudioKind.NONE
        return AudioEvent(header, kind, confidence, window.window_ms, self.version)
