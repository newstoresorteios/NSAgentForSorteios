"""LLM domain: OpenAI gateway, prompts, presenter."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional LLM paths from failing the turn, but do not hide the type."""
    print(f"[llm.{scope}]", {"error_type": type(exc).__name__})
