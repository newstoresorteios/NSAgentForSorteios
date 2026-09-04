"""Memory domain: contact prefs, working memory, proposals, history."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional memory paths from failing the turn, but do not hide the type."""
    print(f"[memory.{scope}]", {"error_type": type(exc).__name__})
