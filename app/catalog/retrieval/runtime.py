"""Shared settings lookup so tests can patch one place."""

from __future__ import annotations

from app.config import get_settings

__all__ = ["get_settings", "log_swallowed"]


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional catalog paths from failing the turn, but do not hide the type."""
    print(f"[catalog.{scope}]", {"error_type": type(exc).__name__})
