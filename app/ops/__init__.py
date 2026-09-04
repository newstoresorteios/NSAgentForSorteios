"""Ops domain: lock, takeover, observability, rollout."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional ops paths from failing the request, but do not hide the type."""
    print(f"[ops.{scope}]", {"error_type": type(exc).__name__})
