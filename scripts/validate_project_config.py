"""Fail CI when documented environment variables drift from Settings."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings


def main() -> int:
    documented = set(
        re.findall(
            r"(?m)^([A-Z][A-Z0-9_]*)=",
            (ROOT / ".env.example").read_text(encoding="utf-8-sig"),
        )
    )
    aliases = {
        str(field.alias)
        for field in Settings.model_fields.values()
        if field.alias
    }
    missing = sorted(aliases - documented)
    if missing:
        print("undocumented Settings aliases:", ", ".join(missing))
        return 1
    print(f"project config: OK ({len(aliases)} Settings aliases documented)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
