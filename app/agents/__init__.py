"""Agent package: domain door, commerce route table, and double-check entry.

Imports stay lazy so ``import app.agents`` does not load sales.
Guardrails and judges stay in ``app.verify``. Catalog retrieval stays
on ``app.sales_agent``; ``handle_sales_message`` lives on commerce.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "apply_double_check",
    "apply_double_check_async",
    "build_agent_input",
    "generate_agent_reply",
    "generate_agent_reply_async",
    "generate_clarification_reply",
    "handle_sales_message",
    "interpret_message",
    "interpretation_to_plan",
    "sales_response_with_openai",
]

_DOOR_EXPORTS = frozenset(
    {
        "build_agent_input",
        "generate_agent_reply",
        "generate_agent_reply_async",
    }
)
_COMMERCE_EXPORTS = frozenset(
    {
        "generate_clarification_reply",
        "handle_sales_message",
        "interpret_message",
        "interpretation_to_plan",
        "sales_response_with_openai",
    }
)
_DOUBLE_CHECK_EXPORTS = frozenset(
    {"apply_double_check", "apply_double_check_async"}
)


def __getattr__(name: str) -> Any:
    if name in _DOOR_EXPORTS:
        from app.agents import door as door_mod

        return getattr(door_mod, name)
    if name in _COMMERCE_EXPORTS:
        from app.agents import commerce as commerce_mod

        return getattr(commerce_mod, name)
    if name in _DOUBLE_CHECK_EXPORTS:
        from app.agents import double_check as double_check_mod

        return getattr(double_check_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
