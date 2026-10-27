"""Small depthwise CNN and optional PyTorch/ONNX inference backends."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .features import LogMelConfig


try:
    import torch
    from torch import nn
except ImportError:  # Optional on the Pi/controller and developer machines.
    torch = None
    nn = None


def require_torch() -> tuple[Any, Any]:
    if torch is None or nn is None:
        raise RuntimeError(
            "PyTorch is required for model training/checkpoint inference; "
            "install the 'audio-train' optional dependencies"
        )
    return torch, nn


if nn is not None:

    class _DepthwiseBlock(nn.Module):
        def __init__(self, input_channels: int, output_channels: int, stride: int) -> None:
            super().__init__()
            self.layers = nn.Sequential(
                nn.Conv2d(
                    input_channels,
                    input_channels,
                    kernel_size=3,
                    stride=stride,
                    padding=1,
                    groups=input_channels,
                    bias=False,
                ),
                nn.BatchNorm2d(input_channels),
                nn.SiLU(inplace=True),
                nn.Conv2d(input_channels, output_channels, kernel_size=1, bias=False),
                nn.BatchNorm2d(output_channels),
                nn.SiLU(inplace=True),
            )

        def forward(self, inputs):
            return self.layers(inputs)


    class AudioEventNet(nn.Module):
        """TensorRT-friendly sub-100k-parameter log-mel classifier."""

        def __init__(self, output_count: int = 2) -> None:
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 16, kernel_size=3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(16),
                nn.SiLU(inplace=True),
                _DepthwiseBlock(16, 24, 2),
                _DepthwiseBlock(24, 32, 2),
                _DepthwiseBlock(32, 48, 2),
                _DepthwiseBlock(48, 64, 2),
                nn.AdaptiveAvgPool2d(1),
            )
            self.classifier = nn.Sequential(nn.Flatten(), nn.Dropout(0.2), nn.Linear(64, output_count))

        def forward(self, inputs):
            return self.classifier(self.features(inputs))

else:

    class AudioEventNet:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            require_torch()


def _torch_load(path: Path, map_location: str) -> dict[str, Any]:
    framework, _ = require_torch()
    try:
        value = framework.load(path, map_location=map_location, weights_only=False)
    except TypeError:
        value = framework.load(path, map_location=map_location)
    if not isinstance(value, dict):
        raise ValueError("audio checkpoint root must be a dictionary")
    return value


class TorchCheckpointBackend:
    def __init__(self, checkpoint_path: str | Path, *, device: str = "cpu") -> None:
        framework, _ = require_torch()
        self.path = Path(checkpoint_path).expanduser().resolve()
        checkpoint = _torch_load(self.path, device)
        self.model_version = str(checkpoint.get("model_version") or self.path.stem)
        self.feature_config = LogMelConfig(**(checkpoint.get("feature_config") or {}))
        self.thresholds = tuple(checkpoint.get("thresholds") or (0.72, 0.72))
        self.device = framework.device(device)
        self.model = AudioEventNet()
        self.model.load_state_dict(checkpoint["model_state"])
        self.model.to(self.device).eval()

    def predict_probabilities(self, feature: np.ndarray) -> tuple[float, float]:
        framework, _ = require_torch()
        inputs = framework.from_numpy(np.asarray(feature, dtype=np.float32)[np.newaxis]).to(
            self.device
        )
        with framework.inference_mode():
            probabilities = framework.sigmoid(self.model(inputs))[0].cpu().numpy()
        return float(probabilities[0]), float(probabilities[1])


class OnnxRuntimeBackend:
    def __init__(self, model_path: str | Path) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("onnxruntime is required for ONNX inference") from exc
        self.path = Path(model_path).expanduser().resolve()
        sidecar = self.path.with_suffix(".json")
        metadata = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
        self.model_version = str(metadata.get("model_version") or self.path.stem)
        self.feature_config = LogMelConfig(**(metadata.get("feature_config") or {}))
        self.thresholds = tuple(metadata.get("thresholds") or (0.72, 0.72))
        self.session = ort.InferenceSession(str(self.path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    def predict_probabilities(self, feature: np.ndarray) -> tuple[float, float]:
        inputs = np.asarray(feature, dtype=np.float32)[np.newaxis]
        logits = np.asarray(self.session.run(None, {self.input_name: inputs})[0])[0]
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        return float(probabilities[0]), float(probabilities[1])


def load_audio_backend(model_path: str | Path, *, device: str = "cpu"):
    """Load a training checkpoint or deployment ONNX model by file suffix."""

    path = Path(model_path).expanduser().resolve()
    if path.suffix.lower() == ".pt":
        return TorchCheckpointBackend(path, device=device)
    if path.suffix.lower() == ".onnx":
        return OnnxRuntimeBackend(path)
    raise ValueError("model must be a .pt checkpoint or .onnx file")


def export_checkpoint_to_onnx(
    checkpoint_path: str | Path,
    output_path: str | Path,
    *,
    opset_version: int = 17,
) -> Path:
    framework, _ = require_torch()
    source = Path(checkpoint_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = _torch_load(source, "cpu")
    config = LogMelConfig(**(checkpoint.get("feature_config") or {}))
    model = AudioEventNet()
    model.load_state_dict(checkpoint["model_state"])
    model.eval()
    example = framework.zeros(1, 1, config.n_mels, config.target_frames, dtype=framework.float32)
    export_options = {
        "input_names": ["log_mel"],
        "output_names": ["logits"],
        "dynamic_axes": {"log_mel": {0: "batch"}, "logits": {0: "batch"}},
        "opset_version": opset_version,
    }
    try:
        framework.onnx.export(model, example, output, dynamo=False, **export_options)
    except TypeError:
        framework.onnx.export(model, example, output, **export_options)
    metadata = {
        "model_version": str(checkpoint.get("model_version") or source.stem),
        "feature_config": asdict(config),
        "thresholds": list(checkpoint.get("thresholds") or (0.72, 0.72)),
        "target_names": ["siren", "horn"],
    }
    output.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


__all__ = [
    "AudioEventNet",
    "OnnxRuntimeBackend",
    "TorchCheckpointBackend",
    "export_checkpoint_to_onnx",
    "load_audio_backend",
    "require_torch",
]
