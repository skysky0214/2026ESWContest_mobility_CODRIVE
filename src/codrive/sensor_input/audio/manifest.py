"""Build a leak-resistant manifest from the AI Hub city-sound archives."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from .archive import NestedZipArchive, repair_zip_name


SIREN_LABEL = "siren"
HORN_LABEL = "horn"
BACKGROUND_LABEL = "background"
TARGET_NAMES = (SIREN_LABEL, HORN_LABEL)


@dataclass(frozen=True, slots=True)
class AudioManifestRecord:
    split: str
    partition: str
    source_archive: str
    zip_member: str
    decoded_member: str
    label_member: str
    source_recording: str
    group_id: str
    label: str
    siren_target: int
    horn_target: int
    category_01: str
    category_02: str
    category_03: str
    subcategory: str
    duration_s: float
    decibel: float | None
    city: str
    district: str
    place: str
    weather: str
    direction: str
    distance: str
    acquisition_device: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AudioManifestRecord":
        return cls(**value)


@dataclass(frozen=True, slots=True)
class _ArchivePair:
    split: str
    label_archive: Path
    source_archive: Path


def _find_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected exactly one {pattern!r} in {root}, found {matches}")
    return matches[0]


def discover_archive_pairs(dataset_root: str | Path) -> tuple[_ArchivePair, ...]:
    root = Path(dataset_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    return (
        _ArchivePair(
            split="training",
            label_archive=_find_one(root, "TL_1.*.tar"),
            source_archive=_find_one(root, "TS_1.*.tar"),
        ),
        _ArchivePair(
            split="official_validation",
            label_archive=_find_one(root, "VL_1.*.tar"),
            source_archive=_find_one(root, "VS_1.*.tar"),
        ),
    )


def _stable_partition(split: str, group_id: str, tune_fraction: float) -> str:
    if split == "official_validation":
        return "final_test"
    digest = hashlib.sha256(group_id.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return "tune" if unit < tune_fraction else "train"


def _event_label(category_03: str, subcategory: str) -> tuple[str, int, int]:
    text = f"{category_03} {subcategory}"
    if "사이렌" in text:
        return SIREN_LABEL, 1, 0
    if "경적" in text:
        return HORN_LABEL, 0, 1
    return BACKGROUND_LABEL, 0, 0


def _source_index(source: NestedZipArchive) -> dict[str, list[tuple[str, str]]]:
    by_basename: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for raw_name in source.file_names(suffix=".wav"):
        repaired = repair_zip_name(raw_name)
        by_basename[PurePosixPath(repaired).name].append((raw_name, repaired))
    return dict(by_basename)


def _select_source_member(
    candidates: list[tuple[str, str]], *, category_02: str, category_03: str
) -> tuple[str, str]:
    if len(candidates) == 1:
        return candidates[0]
    matching = [
        candidate
        for candidate in candidates
        if category_02 in candidate[1] and category_03 in candidate[1]
    ]
    if len(matching) == 1:
        return matching[0]
    raise ValueError(
        f"cannot uniquely match WAV among {len(candidates)} candidates for "
        f"{category_02}/{category_03}"
    )


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _records_for_pair(pair: _ArchivePair, tune_fraction: float) -> Iterator[AudioManifestRecord]:
    with NestedZipArchive(pair.label_archive) as labels, NestedZipArchive(
        pair.source_archive
    ) as source:
        sources = _source_index(source)
        for label_member in labels.file_names(suffix=".json"):
            payload = json.loads(labels.read(label_member).decode("utf-8-sig"))
            audio = payload.get("audio") or {}
            environment = payload.get("environment") or {}
            source_recording = str(audio.get("fileName") or PurePosixPath(label_member).stem)
            group_id = f"{pair.split}:{source_recording}"
            partition = _stable_partition(pair.split, group_id, tune_fraction)
            annotations = payload.get("annotations") or []
            if not isinstance(annotations, list):
                raise ValueError(f"annotations is not an array in {label_member}")
            for annotation in annotations:
                categories = annotation.get("categories") or {}
                category_01 = str(categories.get("category_01") or "")
                category_02 = str(categories.get("category_02") or "")
                category_03 = str(categories.get("category_03") or "")
                subcategory = str(annotation.get("subCategory") or "")
                label_name = str(annotation.get("labelName") or "")
                candidates = sources.get(PurePosixPath(label_name).name)
                if not candidates:
                    raise FileNotFoundError(
                        f"WAV {label_name!r} referenced by {label_member} is absent from "
                        f"{pair.source_archive.name}"
                    )
                raw_member, decoded_member = _select_source_member(
                    candidates, category_02=category_02, category_03=category_03
                )
                area = annotation.get("area") or {}
                start = float(area.get("start") or 0.0)
                end = float(area.get("end") or start)
                label, siren_target, horn_target = _event_label(category_03, subcategory)
                yield AudioManifestRecord(
                    split=pair.split,
                    partition=partition,
                    source_archive=str(pair.source_archive),
                    zip_member=raw_member,
                    decoded_member=decoded_member,
                    label_member=repair_zip_name(label_member),
                    source_recording=source_recording,
                    group_id=group_id,
                    label=label,
                    siren_target=siren_target,
                    horn_target=horn_target,
                    category_01=category_01,
                    category_02=category_02,
                    category_03=category_03,
                    subcategory=subcategory,
                    duration_s=max(0.0, end - start),
                    decibel=_number_or_none(annotation.get("decibel")),
                    city=str(environment.get("urban") or ""),
                    district=str(environment.get("district") or ""),
                    place=str(environment.get("place") or ""),
                    weather=str(environment.get("weather") or ""),
                    direction=str(environment.get("direction") or "unknown"),
                    distance=str(environment.get("distance") or "unknown"),
                    acquisition_device=str(environment.get("acqDevice") or ""),
                )


def build_manifest(
    dataset_root: str | Path,
    *,
    tune_fraction: float = 0.15,
) -> list[AudioManifestRecord]:
    """Scan both official splits and return one row per extracted event WAV."""

    if not 0.0 < tune_fraction < 0.5:
        raise ValueError("tune_fraction must be in (0, 0.5)")
    records: list[AudioManifestRecord] = []
    for pair in discover_archive_pairs(dataset_root):
        records.extend(_records_for_pair(pair, tune_fraction))
    return records


def write_manifest(records: Iterable[AudioManifestRecord], output: str | Path) -> Path:
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return path


def read_manifest(path: str | Path) -> list[AudioManifestRecord]:
    records: list[AudioManifestRecord] = []
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"manifest line {line_number} is not an object")
            records.append(AudioManifestRecord.from_dict(payload))
    return records


def summarize(records: Iterable[AudioManifestRecord]) -> dict[str, Any]:
    rows = list(records)
    return {
        "total": len(rows),
        "labels": dict(sorted(Counter(row.label for row in rows).items())),
        "partitions": dict(sorted(Counter(row.partition for row in rows).items())),
        "categories": dict(sorted(Counter(row.category_03 for row in rows).items())),
        "hours": round(sum(row.duration_s for row in rows) / 3600.0, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/datasets/audio/city_sound_manifest.jsonl"),
    )
    parser.add_argument("--tune-fraction", type=float, default=0.15)
    args = parser.parse_args(argv)

    records = build_manifest(args.dataset_root, tune_fraction=args.tune_fraction)
    output = write_manifest(records, args.output)
    print(json.dumps({"manifest": str(output), **summarize(records)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AudioManifestRecord",
    "BACKGROUND_LABEL",
    "HORN_LABEL",
    "SIREN_LABEL",
    "TARGET_NAMES",
    "build_manifest",
    "discover_archive_pairs",
    "read_manifest",
    "summarize",
    "write_manifest",
]
