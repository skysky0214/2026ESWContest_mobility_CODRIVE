"""Verify source/model identity without importing optional GPU dependencies."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "artifacts/deployment/manifest.json").read_text())
mismatches = []
for name, expected in manifest["files"].items():
    path = root / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        mismatches.append(name)
model = root / "artifacts/models/dms.pt"
model_matches = model.is_file() and hashlib.sha256(model.read_bytes()).hexdigest() == manifest["model_sha256"]
print(json.dumps({"checked_files": len(manifest["files"]), "mismatches": mismatches,
                  "model_matches": model_matches}, ensure_ascii=False))
raise SystemExit(0 if not mismatches and model_matches else 1)
