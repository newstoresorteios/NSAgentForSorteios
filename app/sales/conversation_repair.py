"""Recover a misunderstood request using published copy and existing preferences."""
from __future__ import annotations

import re
from app.catalog.retrieval.text import fold_text
from app.configuration.runtime import message, policy
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def has_explicit_lookup_identity(text: str | None, interpretation: SalesInterpretation | None) -> bool:
    if interpretation is None:
        return False
    folded = fold_text(text)
    subject = interpretation.subject
    for value in (subject.reference, subject.ean):
        if value and re.sub(r"\W", "", fold_text(value)) in re.sub(r"\W", "", folded):
            return True
    return bool(subject.brand and subject.model
                and fold_text(subject.brand) in folded
                and fold_text(subject.model) in folded)


def is_conversation_repair(text: str | None, interpretation: SalesInterpretation | None = None) -> bool:
    if has_explicit_lookup_identity(text, interpretation) and interpretation.resolved_answer_strategy() == "search_catalog":
        return False
    folded = fold_text(text)
    phrases = str(policy("conversationRepairPhrases")).splitlines()
    return any(fold_text(phrase.strip()) in folded for phrase in phrases if phrase.strip())


async def repair_conversation(*, incoming: IncomingMessage, interpretation: SalesInterpretation,
                              state) -> AgentResult | None:
    if not is_conversation_repair(incoming.text, interpretation):
        return None
    from app.ops.handoff_service import build_human_handoff_result
    from app.sales.result_utils import mark_sales_result
    from app.sales.product_lookup import execute_compiled_product_retrieval

    attempt = state.conversation_repair_attempts + 1
    limit = int(policy("conversationRepairHandoffAfter"))
    repaired = interpretation.model_copy(deep=True)
    prefs = state.active_preferences or {}
    repaired.subject.brand = repaired.subject.brand or prefs.get("subject_brand")
    repaired.subject.model = repaired.subject.model or prefs.get("subject_model")
    repaired.subject.reference = repaired.subject.reference or prefs.get("subject_reference")
    repaired.subject.ean = repaired.subject.ean or prefs.get("subject_ean")
    for key in type(repaired.preferences).model_fields:
        if getattr(repaired.preferences, key) in (None, [], "") and prefs.get(key) is not None:
            setattr(repaired.preferences, key, prefs[key])
    if attempt >= limit:
        result = build_human_handoff_result(reason="conversation_repair_failed",
                                           reply_text=message("conversation_repair_handoff"))
    elif repaired.subject.reference or repaired.subject.ean or (repaired.subject.brand and repaired.subject.model):
        repaired.domain = "commerce"
        repaired.goal = "find"
        repaired.answer_strategy = "search_catalog"
        repaired.needs_clarification = False
        repaired.clarification_question = None
        repaired.ready_for_retrieval = repaired.enough_information_to_search = repaired.stop_clarification = True
        repaired.reference_type = None
        repaired.reference_position = None
        # Repair is a read-only search, never a retry of a commercial mutation.
        repaired.purchase_action = repaired.payment_action = repaired.checkout_action = None
        repaired.shipping_action = repaired.order_action = None
        repaired._turn_contract_bound = False
        result = await execute_compiled_product_retrieval(repaired, message_text=incoming.text, commerce_state=state)
        if result is None:
            result = AgentResult(reply_text=message("catalog_unavailable"), intent="commerce",
                                 safety_reason="catalog_unavailable")
        result.reply_text = message("conversation_repair_ack") + "\n\n" + result.reply_text
    else:
        result = AgentResult(reply_text=message("conversation_repair_missing_context"), intent="commerce",
                             safety_reason="conversation_repair")
    original_source = (result.response_metadata or {}).get("response_source")
    result = mark_sales_result(result, interpretation=repaired, goal=repaired.goal,
                               response_source=original_source or "conversation_repair",
                               used_openai_responder=False,
                               used_tray=bool((result.response_metadata or {}).get("used_tray")))
    result.response_metadata["conversation_repair"] = {"attempt": attempt, "handoff_after": limit}
    result.response_metadata["preserve_catalog_context"] = True
    return result
