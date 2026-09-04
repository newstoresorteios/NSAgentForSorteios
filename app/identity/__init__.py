"""Identity domain: sorteio account, person_key, greeting/name heuristics."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional identity paths from failing the turn, but do not hide the type."""
    print(f"[identity.{scope}]", {"error_type": type(exc).__name__})
