"""Criteria for applying conversation summary deltas (not every turn)."""

from __future__ import annotations

from typing import Any

from .memory_models import ConversationSummaryDelta


def should_apply_summary_delta(
    delta: ConversationSummaryDelta | None,
    *,
    existing: dict[str, Any] | None = None,
) -> bool:
    """Return True only when the delta carries meaningful conversation progress."""
    if delta is None:
        return False
    if (delta.last_failure or "").strip():
        return True
    if any(str(item).strip() for item in (delta.user_corrections or [])):
        return True
    if any(str(item).strip() for item in (delta.commitments or [])):
        return True
    if any(str(item).strip() for item in (delta.resolved_points or [])):
        return True
    goal = (delta.current_goal or "").strip()
    if goal:
        previous = str((existing or {}).get("current_goal") or "").strip()
        if goal != previous:
            return True
    open_questions = [
        str(item).strip() for item in (delta.open_questions or []) if str(item).strip()
    ]
    if len(open_questions) >= 2:
        return True
    return False
