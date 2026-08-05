"""Build a clean release ZIP without credentials, caches, or Vercel metadata.

Fails closed if known secret variable names appear inside the packaged tree.
Never prints secret values — only path + variable name.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".vercel",
        ".pytest_cache",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "htmlcov",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "agent-transcripts",
    }
)

EXCLUDE_FILE_GLOBS = (
    "*.py[cod]",
    "*.zip",
    ".coverage",
    "coverage.xml",
    ".DS_Store",
    ".env",
    ".env.*",
)

# Allow .env.example only.
ALLOW_ENV_EXAMPLE = ".env.example"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Non-empty assignment only — empty placeholders in .env.example are OK.
    ("OPENAI_API_KEY", re.compile(r"(?m)^\s*OPENAI_API_KEY\s*=\s*\S+")),
    ("VERCEL_OIDC_TOKEN", re.compile(r"(?m)^\s*VERCEL_OIDC_TOKEN\s*=\s*\S+")),
    ("DATABASE_URL", re.compile(r"(?m)^\s*DATABASE_URL\s*=\s*\S+")),
    ("ADMIN_API_TOKEN", re.compile(r"(?m)^\s*ADMIN_API_TOKEN\s*=\s*\S+")),
    ("BREVO_API_KEY", re.compile(r"(?m)^\s*BREVO_API_KEY\s*=\s*\S+")),
    ("MERCADOPAGO_ACCESS_TOKEN", re.compile(r"(?m)^\s*MERCADOPAGO_ACCESS_TOKEN\s*=\s*\S+")),
    ("MP_ACCESS_TOKEN", re.compile(r"(?m)^\s*MP_ACCESS_TOKEN\s*=\s*\S+")),
    ("CRON_SECRET", re.compile(r"(?m)^\s*CRON_SECRET\s*=\s*\S+")),
)


def _is_excluded_file(rel: Path) -> bool:
    name = rel.name
    if name == ALLOW_ENV_EXAMPLE:
        return False
    for pattern in EXCLUDE_FILE_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return True
    # Hidden env files
    if name.startswith(".env"):
        return True
    return False


def iter_release_files(root: Path) -> list[Path]:
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = set(rel.parts)
        if parts & EXCLUDE_DIR_NAMES:
            continue
        if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if _is_excluded_file(rel):
            continue
        selected.append(path)
    return sorted(selected)


def scan_file_for_secrets(path: Path, *, root: Path) -> list[tuple[str, str]]:
    """Return list of (relative_path, variable_name). Never includes values."""
    hits: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    rel = str(path.relative_to(root)).replace("\\", "/")
    # Skip binary-ish blobs
    if "\x00" in text[:2048]:
        return hits
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            hits.append((rel, name))
    return hits


def validate_no_secrets(files: list[Path], *, root: Path) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for path in files:
        findings.extend(scan_file_for_secrets(path, root=root))
    return findings


def build_zip(
    *,
    root: Path | None = None,
    output: Path | None = None,
    dry_run: bool = False,
) -> Path:
    base = root or REPO_ROOT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = output or (base / "dist" / f"NSAgentForSorteios-release-{stamp}.zip")
    files = iter_release_files(base)
    findings = validate_no_secrets(files, root=base)
    if findings:
        print("ERROR: secret-like assignments found in package candidates:", file=sys.stderr)
        for rel, var_name in findings:
            print(f"  path={rel} variable={var_name}", file=sys.stderr)
        print(
            "Refuse to package. Remove secrets from tracked files "
            "(never commit .env / .env.local / OIDC tokens).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if dry_run:
        print(f"dry-run: would package {len(files)} files -> {out}")
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.relative_to(base).as_posix()
            zf.write(path, arcname)
    print(f"ok: wrote {out} ({len(files)} files)")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output ZIP path (default: dist/NSAgentForSorteios-release-<utc>.zip)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List/validate only; do not write ZIP",
    )
    args = parser.parse_args(argv)
    try:
        build_zip(root=args.root, output=args.output, dry_run=args.dry_run)
    except SystemExit as exc:
        return int(exc.code or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
