# Local Artifacts

Store generated or machine-specific files under this directory:

- `models/vision/`: YOLO, DMS, lane ONNX/TensorRT weights.
- `models/audio/`: audio-classification checkpoints, ONNX and TensorRT weights.
- `datasets/audio/`: generated audio manifests; original AI Hub data stays outside the repo.
- `models/llm/<model-name>/`: GGUF model and that model's benchmark result.
- `recordings/`: recorded sensor streams and replay inputs.
- `logs/`: application logs and latency traces.
- `reports/`: benchmark and post-drive reports.

Create these subdirectories when adding artifacts. Paths in device profiles are
relative to the ESW2026 project root. Artifact contents are ignored by Git except
for the small validated baseline in `models/audio/initial_model/`.
