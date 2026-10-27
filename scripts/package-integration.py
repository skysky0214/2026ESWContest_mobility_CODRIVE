"""Package the reviewed source and models, and record exact deployed hashes."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=Path("/tmp/codrive-integration-20261019.tar.gz"))
args = parser.parse_args()


def include_path(path: Path) -> bool:
    return not any(part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", ".venv-risk"}
                   or part.endswith(".egg-info") for part in path.parts)


manifest = {"base_commit": "a95f92a", "dms_source_commit": "b69ceb3", "files": {}}
manifest["main_source_commit"] = "904e112"
manifest["board_runtime_source_commit"] = "e5104a6"
for directory in ("src", "board_runtime", "config", "scripts", "deploy", "docs"):
    for path in sorted((root / directory).rglob("*")):
        if path.is_file() and include_path(path):
            manifest["files"][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
model = root / "artifacts/models/dms.pt"
manifest["model_sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
manifest["audio_source_commit"] = "490e8194805aee3b50a5e69d1b6ec52812798850"
manifest["risk_source_commit"] = "8545fbb"
manifest["hmi_source_commit"] = "b93538ae36b4cab261de0ee07c5d9a3bd3c03c18"
manifest["front_preview_source_commit"] = manifest["hmi_source_commit"]
manifest["risk_model_sha256"] = hashlib.sha256(
    (root / "artifacts/models/risk/B/model.ubj").read_bytes()
).hexdigest()
for path in sorted((root / "artifacts/audio/initial_model").glob("*")):
    if path.is_file():
        manifest["files"][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
for path in sorted((root / "artifacts/models/risk/B").glob("*")):
    if path.is_file():
        manifest["files"][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest["tts_voice"] = {"engine": "supertonic-3", "language": "ko", "voice": "F1"}
for path in sorted((root / "artifacts/tts/supertonic-ko-f1").glob("*")):
    if path.is_file():
        manifest["files"][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
destination = root / "artifacts/deployment/manifest.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(manifest, indent=2) + "\n")
archive = args.output
with tarfile.open(archive, "w:gz") as tar:
    for name in ("src", "board_runtime", "config", "scripts", "deploy", "docs", "tests", "setup.py", "pytest.ini",
                 "README.md", "artifacts/models/dms.pt", "artifacts/audio/initial_model",
                 "artifacts/models/risk/B", "artifacts/tts/supertonic-ko-f1",
                 "artifacts/deployment/manifest.json"):
        tar.add(root / name, arcname=name,
                filter=lambda item: item if include_path(Path(item.name)) else None)
print(archive)
print("model_sha256", manifest["model_sha256"])
