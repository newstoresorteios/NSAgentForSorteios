"""Verify domain: factual authority, outbound compliance, critique/judge."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional verify paths from failing the turn, but do not hide the type."""
    print(f"[verify.{scope}]", {"error_type": type(exc).__name__})
