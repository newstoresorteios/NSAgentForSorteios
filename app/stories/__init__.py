"""Stories domain: Instagram Story matching (rollout off by default)."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional story paths from failing the turn, but do not hide the type."""
    print(f"[stories.{scope}]", {"error_type": type(exc).__name__})
