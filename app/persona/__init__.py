"""Persona domain: runtime policy, knowledge, versioned instructions."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional persona paths from failing the turn, but do not hide the type."""
    print(f"[persona.{scope}]", {"error_type": type(exc).__name__})
