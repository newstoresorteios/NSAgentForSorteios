"""Commerce domain: cart, checkout, orders, PIX."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional commerce paths from failing the turn, but do not hide the type."""
    print(f"[commerce.{scope}]", {"error_type": type(exc).__name__})
