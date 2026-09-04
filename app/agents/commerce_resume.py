"""Payment-link and presented-catalog resumes for the sales handler.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from typing import Any

from app.memory.context_resume import (
    build_pending_payment_resume_result,
    build_presented_catalog_resume_result,
    should_redisplay_presented_catalog,
    should_resume_pending_order,
)
from app.models import IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


def try_commerce_resume(
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    state: Any,
) -> Any | None:
    sales = _sales()
    if should_resume_pending_order(
        message.text,
        state,
        is_greeting=sales.is_any_greeting(message.text),
    ):
        stored_payment = build_pending_payment_resume_result(state)
        if stored_payment is not None:
            return sales._mark_sales_result(
                stored_payment,
                interpretation=interpretation,
                goal=(interpretation.goal if interpretation is not None else "buy"),
                response_source="context_resume_payment_url",
                used_openai_responder=False,
                used_tray=False,
            )
    if should_redisplay_presented_catalog(message.text, state):
        presented = build_presented_catalog_resume_result(state)
        if presented is not None:
            if interpretation is not None:
                interpretation._clear_pending_action = False
            return sales._mark_sales_result(
                presented,
                interpretation=interpretation,
                goal=(interpretation.goal if interpretation is not None else "find"),
                response_source="context_resume_presented_catalog",
                used_openai_responder=False,
                used_tray=False,
            )
    return None
