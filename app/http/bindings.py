"""Resolve symbols from api.index when tests monkeypatch it."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


def api_index() -> ModuleType | None:
    return sys.modules.get("api.index")


def resolve(name: str, fallback: Any) -> Any:
    loaded = api_index()
    if loaded is not None and hasattr(loaded, name):
        return getattr(loaded, name)
    return fallback
