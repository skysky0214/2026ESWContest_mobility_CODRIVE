"""Extract one labeled AI Hub event WAV for an acoustic speaker/microphone test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import ArchiveAudioStore
from .manifest import read_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--label", choices=("siren", "horn", "background"), default="siren")
    parser.add_argument(
        "--partition", choices=("train", "tune", "final_test"), default="final_test"
    )
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.index < 0:
        parser.error("index must be non-negative")

    candidates = [
        row
        for row in read_manifest(args.manifest)
        if row.partition == args.partition and row.label == args.label
    ]
    if args.index >= len(candidates):
        parser.error(f"index {args.index} is outside {len(candidates)} matching records")
    record = candidates[args.index]
    store = ArchiveAudioStore()
    try:
        payload = store.read(record)
    finally:
        store.close()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    print(
        json.dumps(
            {
                "output": str(output),
                "label": record.label,
                "member": record.decoded_member,
                "duration_s": record.duration_s,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
