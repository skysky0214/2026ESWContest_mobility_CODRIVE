"""Export a trained audio checkpoint to a TensorRT-compatible ONNX graph."""

from __future__ import annotations

import argparse
from pathlib import Path

from .model import export_checkpoint_to_onnx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/models/audio/model/audio_event.onnx"),
    )
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args(argv)
    output = export_checkpoint_to_onnx(args.checkpoint, args.output, opset_version=args.opset)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
