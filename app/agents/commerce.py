"""Commerce agent: sales route table.

Interpret / catalog retrieval stay on ``sales_agent`` so monkeypatches
keep working. This module owns ``handle_sales_message`` and the early
exits (resume, confirmation, objection, checkout).
"""

from __future__ import annotations

import html
from typing import Any

from app.channels.audio_service import (
    audio_transcription_failed_result,
    inbound_audio_failed,
)
from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.interpreter import interpretation_to_plan
from app.sales.responder import (
    generate_clarification_reply,
    sales_response_with_openai,
)

from app.agents.commerce_order import (
    try_commerce_checkout_routes,
    try_commerce_confirmation,
    try_commerce_objection,
)
from app.agents.commerce_resume import try_commerce_resume

__all__ = [
    "OUT_OF_SCOPE_REPLY",
    "deterministic_scope",
    "generate_clarification_reply",
    "handle_sales_message",
    "handle_sales_message_inner",
    "interpret_message",
    "interpretation_to_plan",
    "sales_response_with_openai",
    "try_commerce_checkout_routes",
    "try_commerce_confirmation",
    "try_commerce_objection",
    "try_commerce_resume",
]

_SALES_REEXPORTS = frozenset(
    {
        "OUT_OF_SCOPE_REPLY",
        "deterministic_scope",
        "interpret_message",
    }
)


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


def __getattr__(name: str) -> Any:
    if name in _SALES_REEXPORTS:
        return getattr(_sales(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def handle_sales_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    sales = _sales()
    history_token = sales._sales_recent_turns.set(recent_turns)
    try:
        return await handle_sales_message_inner(
            message,
            facts,
            customer_context,
            semantic_plan=semantic_plan,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
    finally:
        sales._sales_recent_turns.reset(history_token)


async def handle_sales_message_inner(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    sales = _sales()
    if inbound_audio_failed(message):
        return audio_transcription_failed_result()

    interpretation = sales._hydrate_sales_interpretation(
        semantic_plan, message, recent_turns, commerce_state=commerce_state
    )
    state = commerce_state or CommerceConversationState()
    resume = try_commerce_resume(message, interpretation, state)
    if resume is not None:
        return resume
    if interpretation is not None:
        from app.sales.purchase_selection import repair_presented_purchase_selection

        interpretation = repair_presented_purchase_selection(
            interpretation,
            message_text=message.text,
            state=state,
            recent_turns=recent_turns,
        )
        from app.sales.answer_council import apply_turn_contract_for_search

        interpretation = apply_turn_contract_for_search(
            interpretation,
            message_text=message.text,
            commerce_state=state,
        )
    confirmed = await try_commerce_confirmation(message, state)
    if confirmed is not None:
        return confirmed
    objection = try_commerce_objection(message, interpretation, state)
    if objection is not None:
        return objection

    sales.log_purchase_progress("interpretation", "start")
    if interpretation is not None:
        plan = sales.interpretation_to_plan(interpretation, message.text)
    elif isinstance(semantic_plan, SalesInterpretation):
        plan = sales.interpretation_to_plan(semantic_plan, message.text)
    elif semantic_plan and semantic_plan.get("domain") == "commerce":
        plan = semantic_plan
    else:
        plan = await sales.plan_sales_request(message)
    if not plan:
        sales.log_purchase_progress(
            "interpretation",
            "blocked",
            "sales_plan_missing",
        )
        return None
    sales.log_purchase_progress("interpretation", "success")
    if (
        interpretation is not None
        and interpretation.active_topic == "purchase_option_choice"
        and interpretation.needs_clarification
        and str(interpretation.clarification_question or "").strip()
    ):
        return sales._mark_sales_result(
            AgentResult(
                reply_text=html.unescape(
                    str(interpretation.clarification_question).strip()
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="purchase_option_choice",
                response_metadata={"domain": "commerce"},
            ),
            interpretation=interpretation,
            goal="buy",
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_option_choice",
        )
    if interpretation is not None:
        print("[sales.semantic.result]", {
            "scope_domain": interpretation.domain,
            "intent": plan.get("intent"),
            "goal": interpretation.goal,
            "reference_type": interpretation.reference_type,
            "has_subject": bool(
                interpretation.subject.product_type
                or interpretation.subject.brand
                or interpretation.subject.model
                or interpretation.subject.reference
                or interpretation.subject.ean
            ),
            "purchase_action": interpretation.purchase_action,
            "product_action": interpretation.product_action,
            "payment_action": interpretation.payment_action,
            "checkout_channel_preference": interpretation.checkout_channel_preference,
            "image_request": interpretation.image_request,
            "confirmation": interpretation.confirmation,
            "pending_action_disposition": (
                interpretation.confirmation
                if state.pending_action
                else "none"
            ),
        })
    print("[sales.purchase.orchestrator]", {
        "has_purchase_action": bool(
            interpretation and interpretation.purchase_action
        ),
        "has_payment_action": bool(
            interpretation and interpretation.payment_action
        ),
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(
            interpretation.purchase_items
            if interpretation is not None
            else []
        ),
        "reference_type": (
            interpretation.reference_type
            if interpretation is not None
            else None
        ),
        "reference_position_present": bool(
            interpretation
            and interpretation.reference_position is not None
        ),
        "confirmation": (
            interpretation.confirmation
            if interpretation is not None
            else None
        ),
        "has_pending_action": bool(state.pending_action),
        "current_purchase_stage": state.purchase_stage,
    })
    checkout = await try_commerce_checkout_routes(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
    )
    if checkout is not None:
        return checkout
    if (
        interpretation is not None
        and state.pending_action
        and state.pending_action != "awaiting_payment"
        and interpretation.confirmation == "none"
    ):
        interpretation._clear_pending_action = True
    return await sales._handle_sales_catalog_inner(
        message,
        facts,
        customer_context,
        interpretation=interpretation,
        plan=plan,
        state=state,
        recent_turns=recent_turns,
    )
