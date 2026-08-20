"""Separate operational conversation history from the model prompt window.

IQ-09: one canonical model window (``agent_history_limit``).
``agent_max_recent_turns`` is kept as a deprecated alias and synced at settings load.
"""

from __future__ import annotations

from typing import Any


def resolve_history_hard_cap(settings: Any | None = None) -> int:
    """DB / operational recovery bound (not the LLM prompt window)."""
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    try:
        value = int(getattr(settings, "agent_history_hard_cap", 80) or 80)
    except (TypeError, ValueError):
        value = 80
    return max(8, min(200, value))


def resolve_model_history_limit(settings: Any | None = None) -> int:
    """Canonical turns sent to interpreter / responder / persona / critique.

    Prefer ``agent_history_limit``. Fall back to ``agent_max_recent_turns`` only when
    history_limit is missing (legacy callers / partial stubs in tests).
    """
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    raw = getattr(settings, "agent_history_limit", None)
    if raw is None:
        raw = getattr(settings, "agent_max_recent_turns", 12)
    try:
        value = int(raw or 12)
    except (TypeError, ValueError):
        value = 12
    hard_cap = resolve_history_hard_cap(settings)
    return max(4, min(hard_cap, value))


def select_model_history_turns(
    turns: list[dict[str, Any]] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the newest ``limit`` turns for LLM prompts.

    Operational recovery should keep the full loaded window (hard cap).
    The model only receives this sliced list.
    """
    if not turns:
        return []
    safe_limit = max(0, int(limit))
    if safe_limit <= 0:
        return []
    return list(turns)[-safe_limit:]


def count_user_assistant_turns(turns: list[dict[str, Any]] | None) -> dict[str, int]:
    values = turns or []
    return {
        "total": len(values),
        "user": sum(1 for turn in values if turn.get("role") == "user"),
        "assistant": sum(1 for turn in values if turn.get("role") == "assistant"),
    }
