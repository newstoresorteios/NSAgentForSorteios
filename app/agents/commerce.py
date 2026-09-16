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

    from app.sales.conversation_preflight import preflight_reply, normalize_identity
    priority = preflight_reply(message.text)
    if priority is not None:
        return priority

    interpretation = sales._hydrate_sales_interpretation(
        semantic_plan, message, recent_turns, commerce_state=commerce_state
    )
    interpretation = normalize_identity(interpretation)
    state = commerce_state or CommerceConversationState()
    from app.sales.purchase_selection import recover_purchase_target_for_checkout
    state = recover_purchase_target_for_checkout(
        interpretation,
        message_text=message.text,
        state=state,
    )
    from app.sales.contextual_questions import (normalize_followup, try_contextual_question,
        normalize_ready_requirement, recover_mentioned_product, try_availability_question)
    interpretation, state = normalize_followup(message.text, interpretation, state, recent_turns)
    interpretation = normalize_ready_requirement(message.text, interpretation, state, recent_turns)
    from app.sales.contextual_questions import try_name_answer
    name_answer = try_name_answer(message, interpretation, state)
    if name_answer is not None:
        return name_answer
    prior_target = state.active_product
    state = await recover_mentioned_product(message, state, recent_turns, interpretation)
    if interpretation and not prior_target and state.active_product:
        from app.sales.conversation_repair import has_explicit_lookup_identity
        if (interpretation.references_previous_context
                and not has_explicit_lookup_identity(message.text, interpretation)):
            interpretation=interpretation.model_copy(deep=True)
            interpretation.reference_type='current_product'
            interpretation.reference_position=None
    if interpretation and not prior_target and state.active_product and interpretation.goal is None:
        # Restore a pending read-only media request after an acknowledgement or
        # complaint, only when the history yielded one live-verified product.
        import json, re
        from app.configuration.runtime import policy
        from app.catalog.retrieval.text import fold_text
        rules = json.loads(policy('conversationFollowupRules'))
        topic = fold_text(interpretation.active_topic or '')
        if re.search(rules['productMedia'], topic):
            interpretation = interpretation.model_copy(deep=True)
            interpretation.goal = 'inspect'
            interpretation.answer_strategy = 'search_catalog'
            interpretation.image_request = bool(re.search(rules['productPhoto'], topic))
            interpretation.product_action = None if interpretation.image_request else 'get_product_link'
    if interpretation is not None:
        from app.sales.conversation_repair import repair_conversation
        repair = await repair_conversation(incoming=message, interpretation=interpretation, state=state, recent_turns=recent_turns)
        if repair is not None:
            return repair
    if interpretation is not None and interpretation.resolved_answer_strategy() == "handoff":
        from app.ops.handoff_service import build_human_handoff_result

        # The interpreter has already applied the published persona policy.
        # A model mentioned in an appraisal request does not authorize catalog retrieval.
        return build_human_handoff_result(reason="persona_policy_handoff")
    from app.sales.product_comparison import try_product_comparison
    comparison = await try_product_comparison(message, interpretation, state, recent_turns)
    if comparison is not None:
        return comparison
    availability = await try_availability_question(message, interpretation, state, recent_turns)
    if availability is not None:
        return availability
    question = await try_contextual_question(message, interpretation, state, recent_turns)
    if question is not None:
        return question
    if interpretation is not None:
        from app.sales.purchase_selection import repair_presented_purchase_selection
        interpretation = repair_presented_purchase_selection(
            interpretation, message_text=message.text, state=state, recent_turns=recent_turns)
    resume = try_commerce_resume(message, interpretation, state)
    if resume is not None:
        return resume
    if interpretation is not None:
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
