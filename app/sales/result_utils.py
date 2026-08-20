"""Shared sales result metadata helpers."""

from __future__ import annotations

from ..models import AgentResult, SalesInterpretation


def mark_sales_result(
    result: AgentResult,
    *,
    interpretation: SalesInterpretation | None,
    goal: str | None,
    response_source: str,
    used_openai_responder: bool,
    used_tray: bool,
    fallback_reason: str | None = None,
) -> AgentResult:
    interpreter_source = interpretation._source if interpretation else None
    marked = result.with_response_metadata(
        domain="commerce",
        goal=goal,
        response_source=response_source,
        used_openai_interpreter=interpreter_source == "openai",
        used_openai_responder=used_openai_responder,
        used_tray=used_tray,
        fallback_reason=fallback_reason
        or (interpretation._fallback_reason if interpretation else None),
    )
    if interpretation is not None:
        if interpretation._clear_pending_action:
            marked.response_metadata["clear_pending_action"] = True
        marked.response_metadata.setdefault("active_topic", interpretation.active_topic)
        marked.response_metadata.setdefault(
            "purchase_stage", interpretation.purchase_stage
        )
        marked.response_metadata.setdefault(
            "active_preferences",
            interpretation.preferences.model_dump(mode="json", exclude_none=True),
        )
    return marked
