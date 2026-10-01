"""Stable version fingerprints attached to every persisted evaluation run."""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
import subprocess


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=4)
def source_tree_hash(root: str) -> str:
    """Fingerprint runtime Python source even when a deployment omits .git."""
    base = Path(root).resolve()
    app_dir = base / "app"
    digest = hashlib.sha256()
    if not app_dir.is_dir():
        return "unavailable"
    for path in sorted(app_dir.rglob("*.py")):
        relative = path.relative_to(base).as_posix()
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_revision(root: Path) -> str:
    for name in ("VERCEL_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() or "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def build_version_manifest(*, persona_version, bundle, model, judge_model=None,
                           case_hash=None, mode=None, extra=None, root=None) -> dict:
    """Record code, tenant configuration, model and case identity together."""
    repository_root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    values = bundle.get("values") if isinstance(bundle, dict) else {}
    values = values if isinstance(values, dict) else {}
    version = bundle.get("version") if isinstance(bundle, dict) else None
    manifest = {
        "manifest_version": 1,
        "persona": persona_version,
        "configuration": version,
        "configuration_hash": _digest(values),
        "model": model or "unavailable",
        "judge_model": judge_model or "unavailable",
        "case": case_hash or "unavailable",
        "code": _source_revision(repository_root),
        "source_hash": source_tree_hash(str(repository_root)),
        "deployment": os.getenv("VERCEL_URL") or "local",
        "mode": mode or "unspecified",
    }
    if extra:
        manifest.update(extra)
    return manifest
