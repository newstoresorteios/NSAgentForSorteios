from __future__ import annotations

from app.configuration.runtime import message as operator_message

import asyncio
import json
import re
import html
import unicodedata
from contextvars import ContextVar
from typing import Any

_sales_recent_turns: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "sales_recent_turns",
    default=None,
)

from openai import APIError, BadRequestError
from pydantic import ValidationError

from app.commerce.commerce_router import (
    extract_product_query,
    handle_commerce_message,
    is_listed_catalog_follow_up,
    is_outbound_catalog_image_request,
    resolve_commerce_action,
    wants_all_listed_product_images,
    _product_lines,
)
from app.channels.audio_service import (
    audio_transcription_failed_result,
    inbound_audio_failed,
)
from app.catalog.category.resolver import CategoryResolver
from app.commerce.checkout_service import checkout_capabilities, select_checkout_channel
from app.commerce.checkout_data_service import (
    enrich_checkout_data_from_cep,
    repair_checkout_data_with_openai,
    should_repair_checkout_data,
    update_checkout_data,
)
from app.commerce.cart_service import (
    CartItemRequest,
    create_cart_checkout,
    create_cart_items_checkout,
    current_cart_reply,
    log_purchase_progress,
    rebuild_cart_without,
    resolve_cart_item_reference,
    set_cart_item_quantity,
)
from app.commerce.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    checkout_missing_fields,
    evolve_commerce_state,
    product_reference_from_product,
    resolve_commerce_reference,
    resolve_purchase_item_reference,
)
from app.channels.channel_profiles import channel_system_hint
from .config import get_settings
from app.memory.working_memory import WORKING_MEMORY_USAGE_POLICY, build_working_memory
from app.verify.guardrails import (
    detect_commerce_inquiry,
    detect_current_raffle_inquiry,
    detect_raffle_history_inquiry,
    detect_rules_inquiry,
    detect_balance_inquiry,
    detect_coupon_code_inquiry,
)
from .models import AgentResult, IncomingMessage, SalesInterpretation
from app.memory.context_resume import is_short_affirmation
from app.identity.greeting_policy import GREETING_REPLY, is_any_greeting, is_greeting_message
from app.ops.turn_runtime import LLMCallBudgetExceeded
from app.commerce.payment_service import (
    inspect_current_cart,
    inspect_order_payment,
    inspect_payment_options,
)
from app.commerce.pix_checkout_service import (
    generate_direct_pix_checkout,
    refresh_direct_pix_checkout,
    should_use_direct_pix,
)
from app.commerce.shipping_service import list_shipping_methods, quote_shipping, select_shipping
from app.commerce.order_service import (
    confirm_prepared_order,
    create_order,
    get_order_facts,
    prepare_order,
)
from app.catalog.media.product_media import resolve_presented_product_images, resolve_product_image
from app.catalog.product_retrieval import (
    CUSTOMER_RESULT_LIMIT,
    apply_persona_presentation_order,
    commercial_availability_facts,
    ProductMatchError,
    ProductRetrievalCompiler,
    customer_result_limit,
    enrich_product_variants,
    exact_progress_matches,
    exact_specific_product_matches,
    hard_filter_products,
    identity_core_tokens,
    infer_family_codes_from_candidates,
    match_specific_products,
    preference_color_search_labels,
    preference_color_tokens,
    prefilter_specific_candidates,
    product_matches_color_tokens,
    product_availability_state,
    revalidate_products,
    rerank_products,
    score_catalog_candidates,
    semantic_preferences,
    soft_confirm_candidates,
    specific_product_search_terms,
)
from app.catalog.index.cache import ensure_brand_pool_in_candidates
from app.tray.tray_tools import execute_tool


def SALES_PLANNER_INSTRUCTIONS():
    return operator_message('instructions.sales_planner_instructions.3e5d0d4949').strip()

def _operator_sales_responder_instructions_1():
    return operator_message('instructions._operator_sales_responder_instructions_1.c5bfd84014').strip()

# Keep the common sales contract available separately. Checkout instructions
# are large and only belong in turns that can actually advance a purchase.
def BASE_SALES_RESPONDER_INSTRUCTIONS():
    return _operator_sales_responder_instructions_1()

def SALES_CLARIFICATION_INSTRUCTIONS():
    return operator_message('instructions.sales_clarification_instructions.f30acad4b3').strip()

def OUT_OF_SCOPE_REPLY():
    return operator_message('instructions.out_of_scope_reply.3b2b6ef6d6')
def _operator_sales_interpreter_instructions_1():
    return operator_message('instructions._operator_sales_interpreter_instructions_1.e36482f8b1').strip()

def CHECKOUT_FLOW_INSTRUCTIONS():
    return operator_message('instructions.checkout_flow_instructions.055922388c').strip()

def SALES_INTERPRETER_INSTRUCTIONS():
    return operator_message('instructions.sales_interpreter_instructions.730a4d19c0', value_1=f'{_operator_sales_interpreter_instructions_1()}', value_2=f'{CHECKOUT_FLOW_INSTRUCTIONS()}')
def SALES_RESPONDER_INSTRUCTIONS():
    return operator_message('instructions.sales_responder_instructions.730a4d19c0', value_1=f'{BASE_SALES_RESPONDER_INSTRUCTIONS()}', value_2=f'{CHECKOUT_FLOW_INSTRUCTIONS()}')

_ACTION_TO_PLAN = {
    "product_search": "product_search",
    "product_price": "price",
    "product_inventory": "inventory",
    "coupon_search": "coupon",
}


def _purchase_close_hold_reply(
    *,
    message: IncomingMessage,
    state: CommerceConversationState | None,
    interpretation: SalesInterpretation | None,
) -> str:
    from app.commerce.checkout_service import checkout_channel_choice_prompt
    from app.identity.greeting_policy import choose_greeting_reply
    from .sales.dialogue_phase import session_in_checkout_phase

    if state is not None and session_in_checkout_phase(state):
        return checkout_channel_choice_prompt(state)
    if is_any_greeting(message.text):
        return choose_greeting_reply(None)
    clarification = str(
        (interpretation.clarification_question if interpretation else None) or ""
    ).strip()
    if clarification:
        return html.unescape(clarification)
    if (
        state is not None
        and not state.cart_session_id
        and (state.last_presented_products or state.active_product is not None)
    ):
        return operator_message('sales_agent._purchase_close_hold_reply.915c8beed7')
    if state is not None and state.cart_session_id:
        return checkout_channel_choice_prompt(state)
    return operator_message('sales_agent._purchase_close_hold_reply.915c8beed7')


def deterministic_scope(text: str | None) -> dict[str, Any]:
    value = (text or "").strip()
    normalized = value.lower()
    if is_greeting_message(value):
        return {"domain": "greeting", "action": "greeting", "_source": "fallback"}
    if detect_balance_inquiry(value) or detect_coupon_code_inquiry(value) or detect_raffle_history_inquiry(value) or detect_current_raffle_inquiry(value) or detect_rules_inquiry(value) or "sorteio" in normalized:
        return {"domain": "raffle", "action": "local_flow", "_source": "fallback"}
    if detect_commerce_inquiry(value) or normalized.startswith(("tem ", "vocês têm ", "voces tem ", "vende ")) or any(term in normalized for term in ("comprar", "adquirir", "quero ", "procuro", "busco", "orçamento", "orcamento", "comparar", "recomende")):
        plan = deterministic_sales_plan(value) or {}
        return {"domain": "commerce", **plan, "_source": "fallback"}
    store_terms = ("newstore", "new store", "loja", "pedido", "compra", "atendimento comercial", "catálogo", "catalogo")
    if any(term in normalized for term in store_terms):
        return {"domain": "store_general", "action": "store_general", "_source": "fallback"}
    return {"domain": "out_of_scope", "action": "scope_refusal", "_source": "fallback"}


def _normalize_semantic_plan(parsed: dict[str, Any]) -> dict[str, Any] | None:
    domain = parsed.get("domain")
    if domain not in {"commerce", "raffle", "greeting", "store_general", "out_of_scope"}:
        return None
    normalized: dict[str, Any] = {"domain": domain, "action": parsed.get("action"), "_source": "openai"}
    if domain != "commerce":
        return normalized
    action = parsed.get("action")
    goal = parsed.get("goal")
    if not action and goal:
        action = {"find": "product_search", "recommend": "recommendation", "compare": "product_comparison", "inspect": "product_price", "buy": "purchase_intent", "discover": "clarification"}.get(goal)
    allowed = {"purchase_intent", "product_search", "recommendation", "product_price", "product_inventory", "product_comparison", "coupon_search", "clarification"}
    if action not in allowed:
        return None
    subject = parsed.get("subject") if isinstance(parsed.get("subject"), dict) else {}
    constraints_input = parsed.get("constraints") if isinstance(parsed.get("constraints"), dict) else {}
    query = subject.get("query") or parsed.get("product_query") or subject.get("product_type") or parsed.get("product_type") or subject.get("model") or parsed.get("model") or subject.get("reference") or parsed.get("reference") or subject.get("ean") or parsed.get("ean") or ""
    filters: dict[str, Any] = {}
    for key in ("brand", "model", "reference", "ean", "budget_min", "budget_max", "attributes"):
        value = subject.get(key) if key in {"brand", "model", "reference", "ean"} else constraints_input.get(key, parsed.get(key))
        if value is not None:
            filters[key] = value
    attributes = constraints_input.get("attributes", parsed.get("attributes"))
    if isinstance(attributes, list) and attributes:
        query = " ".join([str(query), *[str(item) for item in attributes]]).strip()
    normalized.update({
        "intent": action,
        "goal": goal or {"purchase_intent": "buy", "product_search": "find", "recommendation": "recommend", "product_comparison": "compare", "product_price": "inspect", "product_inventory": "inspect", "coupon_search": "inspect", "clarification": "discover"}.get(action),
        "subject": {"product_type": subject.get("product_type") or parsed.get("product_type"), "query": str(query).strip(), "brand": filters.get("brand"), "model": filters.get("model"), "reference": filters.get("reference"), "ean": filters.get("ean")},
        "constraints": {
            "budget_min": filters.get("budget_min"),
            "budget_max": filters.get("budget_max"),
            "attributes": filters.get("attributes") or [],
            "color": constraints_input.get("color"),
            "style": constraints_input.get("style"),
            "material": constraints_input.get("material"),
            "explicit_no_preferences": constraints_input.get("explicit_no_preferences") or [],
        },
        "information_needed": parsed.get("information_needed") or ["catalog"],
        "needs_clarification": bool(parsed.get("needs_clarification")),
        "clarification_question": parsed.get("clarification_question"),
        "query": str(query).strip(),
        "filters": filters,
        "budget_max": parsed.get("budget_max"),
        "product_type": parsed.get("product_type"),
    })
    return normalized


def _parse_scope(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _normalize_semantic_plan(parsed) if isinstance(parsed, dict) else None


def _fallback_interpretation(text: str | None) -> SalesInterpretation:
    legacy = deterministic_scope(text)
    subject = legacy.get("subject") if isinstance(legacy.get("subject"), dict) else {}
    constraints = legacy.get("constraints") if isinstance(legacy.get("constraints"), dict) else {}
    filters = legacy.get("filters") if isinstance(legacy.get("filters"), dict) else {}
    fallback_goal = legacy.get("goal") or {
        "purchase_intent": "buy",
        "product_search": "find",
        "price": "inspect",
        "inventory": "inspect",
        "coupon": "inspect",
        "recommendation": "recommend",
        "product_comparison": "compare",
        "clarification": "discover",
    }.get(legacy.get("intent"))
    interpretation = SalesInterpretation(
        domain=legacy.get("domain", "out_of_scope"),
        goal=fallback_goal,
        subject={
            "product_type": subject.get("product_type") or legacy.get("product_type"),
            "brand": subject.get("brand") or filters.get("brand"),
            "model": subject.get("model") or filters.get("model"),
            "reference": subject.get("reference") or filters.get("reference"),
            "ean": subject.get("ean") or filters.get("ean"),
        },
        preferences={
            "budget_min": constraints.get("budget_min") or filters.get("budget_min"),
            "budget_max": constraints.get("budget_max") or filters.get("budget_max"),
            "color": constraints.get("color") or filters.get("color"),
            "style": constraints.get("style") or filters.get("style"),
            "material": constraints.get("material") or filters.get("material"),
            "attributes": constraints.get("attributes") or filters.get("attributes") or [],
            "explicit_no_preferences": constraints.get("explicit_no_preferences") or [],
        },
        information_needed=["catalog"] if legacy.get("domain") == "commerce" else [],
        references_previous_context=False,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        stop_clarification=False,
        needs_clarification=bool(legacy.get("needs_clarification")),
        clarification_question=legacy.get("clarification_question"),
        confidence=0.6,
    )
    interpretation._source = "deterministic_fallback"
    from app.catalog.specs.preference_normalize import normalize_sales_interpretation
    from app.llm.turn_understanding import sales_to_turn_understanding
    from app.memory.context_resume import scrub_catalog_question_interpretation

    normalized = normalize_sales_interpretation(interpretation, message_text=text)
    normalized = scrub_catalog_question_interpretation(normalized, text)
    normalized._turn_understanding = sales_to_turn_understanding(
        normalized, message_text=text
    )
    return normalized


def _rehydrate_contact_preferences(
    interpretation: SalesInterpretation,
    message: IncomingMessage,
) -> SalesInterpretation:
    """Seed empty preference fields from durable contact memory."""
    try:
        from app.memory.contact_preference_memory import (
            rehydrate_interpretation_from_contact_memory,
        )

        settings = get_settings()
        sender_key = message.sender_key or (
            f"whatsapp:{message.sender_phone}" if message.sender_phone else None
        )
        return rehydrate_interpretation_from_contact_memory(
            interpretation,
            tenant_id=str(getattr(settings, "agent_persona_tenant_id", "newstore")),
            sender_key=sender_key,
            message_text=message.text,
        )
    except Exception as exc:
        print(
            "[memory.contact_preference.rehydrate_hook_error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:120]},
        )
        return interpretation


def _finalize_fallback_interpretation(
    fallback: SalesInterpretation,
    message: IncomingMessage,
) -> SalesInterpretation:
    from app.memory.context_resume import scrub_catalog_question_interpretation

    fallback = _rehydrate_contact_preferences(fallback, message)
    return scrub_catalog_question_interpretation(fallback, message.text)


def _log_interpretation(
    interpretation: SalesInterpretation,
    model: str,
    *,
    fallback_reason: str | None = None,
) -> None:
    preferences = interpretation.preferences
    turn = getattr(interpretation, "_turn_understanding", None)
    payload = {
        "source": interpretation._source,
        "model": model,
        "domain": interpretation.domain,
        "goal": interpretation.goal,
        "confidence": interpretation.confidence,
        "references_previous_context": interpretation.references_previous_context,
        "has_product_type": bool(interpretation.subject.product_type),
        "has_brand": bool(interpretation.subject.brand),
        "has_style": bool(preferences.style),
        "has_color": bool(preferences.color),
        "has_budget": preferences.budget_min is not None or preferences.budget_max is not None,
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "needs_clarification": interpretation.needs_clarification,
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    if turn is not None:
        payload.update(
            {
                "primary_intent": getattr(turn, "primary_intent", None),
                "answer_strategy": getattr(turn, "answer_strategy", None),
                "clarification_required": getattr(turn, "clarification_required", None),
                "hard_brand": bool(getattr(getattr(turn, "hard_constraints", None), "brand", None)),
                "hard_budget_max": getattr(
                    getattr(turn, "hard_constraints", None), "budget_max", None
                ),
                "ambiguity_count": len(getattr(turn, "ambiguity", None) or []),
                "blocking_ambiguity": sum(
                    1
                    for item in (getattr(turn, "ambiguity", None) or [])
                    if getattr(item, "blocking", False)
                ),
            }
        )
    print("[sales.interpreter]", payload)


def _normalize_interpreter_history(
    recent_turns: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    from app.memory.history_window import prefix_turn_sent_at

    normalized: list[dict[str, str]] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        if role in {"user", "assistant"}:
            content = prefix_turn_sent_at(content, turn.get("created_at"))
        normalized.append({"role": role, "content": content})
    return normalized


def _sanitize_openai_error_message(value: object) -> str:
    message = str(value or "OpenAI rejected the interpreter request")
    message = re.sub(r"sk-(?:proj-)?[A-Za-z0-9_-]+", "sk-***", message)
    message = re.sub(r"(?i)(authorization\s*[:=]?\s*bearer)\s+\S+", r"\1 ***", message)
    return message[:600]


def _bad_request_details(exc: BadRequestError, model: str) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    body_error = body.get("error") if isinstance(body.get("error"), dict) else body
    code = getattr(exc, "code", None) or body_error.get("code")
    param = getattr(exc, "param", None) or body_error.get("param")
    message = getattr(exc, "message", None) or body_error.get("message") or str(exc)
    return {
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "error_code": code,
        "error_param": param,
        "error_message": _sanitize_openai_error_message(message),
        "model": model,
    }


from app.sales.interpreter import interpretation_to_plan


def _open_sale_history(commerce_state: CommerceConversationState | None) -> bool:
    from .sales.dialogue_phase import is_open_sale_state

    return is_open_sale_state(commerce_state)


async def interpret_message(
    message: IncomingMessage,
    *,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> SalesInterpretation:
    settings = get_settings()
    from .sales.dialogue_phase import blocks_greeting_fast_path

    if is_greeting_message(message.text) and not blocks_greeting_fast_path(commerce_state):
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "greeting_fast_path"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="greeting_fast_path")
        return fallback
    if not settings.openai_api_key:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_api_key_missing"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_api_key_missing")
        return fallback
    current_text = (message.text or "").strip()
    if not current_text:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "empty_message"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="empty_message")
        return fallback

    from app.llm.capability_catalog import format_capability_catalog_for_prompt
    from app.llm.turn_understanding import (
        TURN_UNDERSTANDING_INSTRUCTIONS,
        TurnUnderstanding,
        apply_clarification_policy,
        sanitize_turn_understanding,
        sales_to_turn_understanding,
        turn_understanding_to_sales,
    )

    from app.ops.rollout import is_turn_understanding_enabled

    use_turn_understanding = is_turn_understanding_enabled(settings)
    if use_turn_understanding:
        interpreter_model = (
            (getattr(settings, "openai_fast_model", None) or "").strip()
            or (getattr(settings, "openai_main_model", None) or "").strip()
            or settings.openai_model
        )
    else:
        interpreter_model = settings.openai_model

    normalized_history = _normalize_interpreter_history(recent_turns)
    state_obj = commerce_state or CommerceConversationState()
    prompt_contract = state_obj.prompt_contract_payload()
    state_message = {
        "role": "system",
        "content": (
            "INTERPRETER_CONTRACT:\n"
            + json.dumps(prompt_contract, ensure_ascii=False)
            + "\n\nCOMMERCE_STATE:\n"
            + json.dumps(state_obj.interpreter_payload(), ensure_ascii=False)
            + "\n\nWORKING_MEMORY:\n"
            + json.dumps(build_working_memory(state_obj), ensure_ascii=False)
            + "\n"
            + WORKING_MEMORY_USAGE_POLICY
            + "\n\n"
            + format_capability_catalog_for_prompt()
        ),
    }
    system_instructions = (
        TURN_UNDERSTANDING_INSTRUCTIONS
        if use_turn_understanding
        else SALES_INTERPRETER_INSTRUCTIONS()
    )
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None and runtime.enabled:
            system_instructions = (
                f"{system_instructions}\n\n{runtime.interpreter_policy_block()}"
            )
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("interpreter.persona_block", exc)
    try:
        from app.persona.store_knowledge import format_institutional_knowledge_block

        knowledge_block = format_institutional_knowledge_block(current_text)
        if knowledge_block:
            system_instructions = f"{system_instructions}\n\n{knowledge_block}"
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("interpreter.institutional_knowledge", exc)
    from app.memory.history_window import HISTORY_TIME_POLICY, prefix_turn_sent_at

    system_instructions = f"{system_instructions}\n\n{HISTORY_TIME_POLICY}"
    messages = [
        {"role": "system", "content": system_instructions},
        state_message,
        *normalized_history,
        {"role": "user", "content": prefix_turn_sent_at(current_text, current=True)},
    ]
    print("[sales.interpreter.request]", {
        "model": interpreter_model,
        "structured_output": True,
        "turn_understanding": use_turn_understanding,
        "history_turns": len(normalized_history),
        "message_count": len(messages),
        "has_temperature": True,
        "has_max_tokens": False,
        "has_tools": False,
    })
    try:
        from app.llm.openai_errors import OpenAIGatewayError, OpenAIRefusalError
        from app.llm.openai_gateway import parse_structured_output
        from app.catalog.specs.preference_normalize import (
            normalize_sales_interpretation,
            recent_user_context_text,
        )

        text_format = TurnUnderstanding if use_turn_understanding else SalesInterpretation
        parse_result = await parse_structured_output(
            model=interpreter_model,
            text_format=text_format,
            messages=messages,
            temperature=0,
            call_type="decision",
        )
        parsed = parse_result.parsed
        if use_turn_understanding:
            if isinstance(parsed, TurnUnderstanding):
                understanding = sanitize_turn_understanding(parsed)
                has_recoverable_reference = bool(
                    getattr(state_obj, "last_presented_products", None)
                    or getattr(state_obj, "active_product", None)
                )
                understanding = apply_clarification_policy(
                    understanding,
                    message_text=current_text,
                    has_recoverable_reference=has_recoverable_reference,
                )
                understanding._source = "openai"
                interpretation = turn_understanding_to_sales(understanding)
            elif isinstance(parsed, SalesInterpretation):
                # Compatibility: legacy schema / test fakes during rollout.
                interpretation = parsed
                interpretation._source = "openai"
                interpretation._turn_understanding = sales_to_turn_understanding(
                    interpretation, message_text=current_text
                )
            else:
                raise ValueError("interpreter_turn_understanding_missing")
        else:
            if not isinstance(parsed, SalesInterpretation):
                raise ValueError("interpreter_schema_missing")
            interpretation = parsed
            interpretation._source = "openai"
            # Shadow: keep a TurnUnderstanding view for metrics / later cutover.
            interpretation._turn_understanding = sales_to_turn_understanding(
                interpretation, message_text=current_text
            )

        from .sales.qualification_slots import (
            continue_commerce_from_qualification_answer,
            rehydrate_qualification_slots_from_turns,
        )

        try:
            interpretation = rehydrate_qualification_slots_from_turns(
                interpretation,
                recent_turns,
                message_text=current_text,
                conversation_id=message.conversation_id,
                include_other_threads=_open_sale_history(commerce_state),
            )
        except Exception as exc:
            from app.sales import log_swallowed

            log_swallowed("interpreter.qual_rehydrate", exc)
        interpretation = normalize_sales_interpretation(
            interpretation,
            message_text=current_text,
            context_text=recent_user_context_text(recent_turns),
            recent_turns=recent_turns,
            conversation_id=message.conversation_id,
            include_other_threads=_open_sale_history(commerce_state),
        )

        interpretation = continue_commerce_from_qualification_answer(
            interpretation,
            recent_turns,
            current_text,
            conversation_id=message.conversation_id,
            include_other_threads=_open_sale_history(commerce_state),
            commerce_state=commerce_state,
        )
        interpretation = _rehydrate_contact_preferences(interpretation, message)
        # Re-sync TurnUnderstanding after preference normalization when present.
        if interpretation._turn_understanding is None:
            interpretation._turn_understanding = sales_to_turn_understanding(
                interpretation, message_text=current_text
            )
        _log_interpretation(interpretation, interpreter_model)
        return interpretation
    except BadRequestError as exc:
        print("[sales.interpreter.error]", _bad_request_details(exc, interpreter_model))
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_bad_request"
        fallback = _finalize_fallback_interpretation(fallback, message)
        _log_interpretation(fallback, interpreter_model, fallback_reason="openai_bad_request")
        return fallback
    except OpenAIRefusalError as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_invalid_response"
        fallback = _finalize_fallback_interpretation(fallback, message)
        _log_interpretation(
            fallback,
            interpreter_model,
            fallback_reason="openai_invalid_response",
        )
        return fallback
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback_reason = "openai_request_failed" if isinstance(exc, APIError) else "openai_invalid_response"
        fallback._fallback_reason = fallback_reason
        fallback = _finalize_fallback_interpretation(fallback, message)
        _log_interpretation(fallback, interpreter_model, fallback_reason=fallback_reason)
        return fallback


def deterministic_sales_plan(text: str | None) -> dict[str, Any] | None:
    normalized = (text or "").lower()
    purchase = any(term in normalized for term in ("quero comprar", "quero adquirir", "quero um ", "quero uma ", "gostaria de comprar", "gostaria de um ", "procuro", "busco", "recomende"))
    action = resolve_commerce_action(text)
    if purchase and not any(term in normalized for term in ("quanto custa", "preço", "preco", "estoque", "disponibilidade")):
        action = "purchase_intent"
    if not action:
        return None
    query = extract_product_query(text)
    budget_max = None
    budget_match = re.search(r"(?:até|ate|por|no máximo|até o limite de)\s*(?:r\$\s*)?([\d.,]+)\s*(mil|k)?", query, flags=re.IGNORECASE)
    if budget_match:
        raw = budget_match.group(1).replace(".", "").replace(",", ".")
        budget_max = float(raw) * (1000 if budget_match.group(2) else 1)
        query = (query[:budget_match.start()] + query[budget_match.end():]).strip(" ,-")
    if query.lower().strip() in {"alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa"}:
        query = ""
    ean_match = re.fullmatch(r"(?:ean\s+)?(\d{8,14})", query, flags=re.IGNORECASE)
    reference = None
    if not ean_match and query and (
        re.search(r"[./_-]", query)
        or (re.search(r"\d", query) and re.search(r"[A-Za-z]", query) and " " not in query)
    ):
        reference = re.sub(r"^(?:sku|ref(?:er[êe]ncia)?)\s+", "", query, flags=re.IGNORECASE)
    fallback_product_type = None
    fallback_model = None
    if query and not ean_match and not reference:
        if action == "product_search":
            fallback_model = query
        else:
            fallback_product_type = query.split()[0] if action == "purchase_intent" else query
    from app.memory.context_resume import is_non_model_query, is_presented_catalog_question

    if is_presented_catalog_question(text) or is_non_model_query(fallback_model or query):
        if re.search(r"rel[oó]gio|watch", str(query or ""), flags=re.IGNORECASE):
            fallback_product_type = fallback_product_type or "relógio"
        fallback_model = None
    from .sales.discovery import _mentioned_watch_brands

    brands = _mentioned_watch_brands(text)
    brand = brands[0] if brands else None
    model = fallback_model
    if brand and model:
        leftover = model.casefold()
        for hit in brands:
            leftover = leftover.replace(hit.casefold(), " ")
        for token in ("relógio", "relogio", "watch"):
            leftover = leftover.replace(token, " ")
        leftover = " ".join(leftover.split())
        model = leftover or None
        if any(token in (fallback_model or "").casefold() for token in ("relógio", "relogio")):
            fallback_product_type = fallback_product_type or "relógio"
    plan: dict[str, Any] = {
        "intent": "purchase_intent" if action == "purchase_intent" else _ACTION_TO_PLAN.get(action, "product_search"),
        "query": query,
        "filters": {"budget_max": budget_max} if budget_max is not None else {},
        "goal": "recommend" if budget_max is not None or (len(query.split()) > 1 and action == "purchase_intent") else ("buy" if action == "purchase_intent" else None),
        "subject": {
            "product_type": fallback_product_type,
            "query": query,
            "ean": ean_match.group(1) if ean_match else None,
            "reference": reference,
        },
        "constraints": {"budget_max": budget_max, "attributes": query.split()[1:] if budget_max is not None and len(query.split()) > 1 else []},
    }
    plan["subject"].update({"brand": brand, "model": model})
    if brand and "brand" not in plan["filters"]:
        plan["filters"] = {**plan["filters"], "brand": brand}
    return plan


def _parse_plan(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    allowed_intents = {"product_search", "price", "inventory", "coupon", "recommendation"}
    intent = parsed.get("intent")
    if intent not in allowed_intents:
        return None
    query = parsed.get("query")
    if query is not None and not isinstance(query, str):
        return None
    filters = parsed.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    return {
        "intent": intent,
        "query": (query or "").strip(),
        "filters": {key: value for key, value in filters.items() if key in {"brand", "category_id", "budget_max", "style", "color"}},
        "budget_max": parsed.get("budget_max"),
    }


async def plan_sales_request(message: IncomingMessage) -> dict[str, Any] | None:
    interpretation = await interpret_message(message)
    if interpretation.domain != "commerce":
        return None
    return interpretation_to_plan(interpretation, message.text)


from .sales.workflows.catalog_ranking import (  # noqa: E402
    rank_candidates,
    score_candidate,
)


_CATALOG_PLAN_INTENTS = frozenset(
    {
        "product_search",
        "product_price",
        "product_inventory",
        "recommendation",
        "price",
        "inventory",
        "product_comparison",
    }
)
_INTENT_TO_GOAL = {
    "product_search": "find",
    "recommendation": "recommend",
    "product_price": "inspect",
    "product_inventory": "inspect",
    "price": "inspect",
    "inventory": "inspect",
    "product_comparison": "compare",
}


def _commerce_plan_to_interpretation(
    plan: dict[str, Any],
) -> SalesInterpretation | None:
    """Turn a catalog-search plan dict into SalesInterpretation for compiled retrieval."""
    if not isinstance(plan, dict) or plan.get("domain") != "commerce":
        return None
    intent = str(plan.get("intent") or plan.get("action") or "")
    goal = plan.get("goal")
    if intent not in _CATALOG_PLAN_INTENTS and goal not in {
        "find",
        "recommend",
        "inspect",
        "compare",
    }:
        return None
    subject = plan.get("subject") if isinstance(plan.get("subject"), dict) else {}
    constraints = plan.get("constraints") if isinstance(plan.get("constraints"), dict) else {}
    filters = plan.get("filters") if isinstance(plan.get("filters"), dict) else {}
    brand = subject.get("brand") or filters.get("brand")
    model = subject.get("model") or filters.get("model")
    reference = subject.get("reference") or filters.get("reference")
    ean = subject.get("ean") or filters.get("ean")
    query = str(subject.get("query") or plan.get("query") or "").strip()
    if not any((brand, model, reference, ean, query)):
        return None
    allowed_goals = {
        "discover",
        "find",
        "recommend",
        "compare",
        "inspect",
        "buy",
        "after_sales",
    }
    resolved_goal = goal if goal in allowed_goals else _INTENT_TO_GOAL.get(intent, "find")
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal=resolved_goal,
            subject={
                "product_type": subject.get("product_type"),
                "brand": brand,
                "model": model,
                "reference": reference,
                "ean": ean,
            },
            preferences={
                "budget_min": constraints.get("budget_min") or filters.get("budget_min"),
                "budget_max": (
                    constraints.get("budget_max")
                    or filters.get("budget_max")
                    or plan.get("budget_max")
                ),
                "color": constraints.get("color") or filters.get("color"),
                "style": constraints.get("style") or filters.get("style"),
                "material": constraints.get("material") or filters.get("material"),
                "attributes": constraints.get("attributes")
                or filters.get("attributes")
                or [],
                "explicit_no_preferences": constraints.get("explicit_no_preferences")
                or [],
            },
            information_needed=plan.get("information_needed") or ["catalog"],
            references_previous_context=False,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=bool(plan.get("needs_clarification")),
            clarification_question=plan.get("clarification_question"),
            confidence=0.8,
        )
    except Exception:
        return None
    source = plan.get("_source")
    if isinstance(source, str) and source:
        interpretation._source = source
    return interpretation


def _ranked_result(result: AgentResult, plan: dict[str, Any]) -> AgentResult | None:
    """Leftover ranking when a plan dict could not be compiled into an interpretation."""
    data = result.commercial_data or {}
    products = data.get("products") if isinstance(data.get("products"), list) else []
    interpretation = _commerce_plan_to_interpretation(plan)
    if interpretation is None or not products:
        return None
    from app.catalog.retrieval.rank_authority import (
        product_rank_ids,
        rank_catalog_products,
        shadow_compare_rank,
    )

    selected = rank_catalog_products(products, interpretation, mode="exact")
    if not selected:
        return None
    try:
        from app.sales.workflows.catalog_ranking import rank_candidates

        leftover = rank_candidates(products, plan, limit=len(selected))
        shadow_compare_rank(
            live_ids=product_rank_ids(selected),
            other_ids=product_rank_ids(leftover),
            other_name="catalog_ranking.rank_candidates",
            mode="leftover",
        )
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("ranked_result.shadow", exc)
    from app.commerce.commerce_router import _product_result

    action = "product_price" if plan.get("intent") == "price" else "product_search"
    ranked = _product_result(action, selected)
    if data.get("inventory") is not None:
        inventory = data["inventory"]
        ranked.reply_text = "Consulta de estoque:\n" + "\n".join(_product_lines(selected, inventory))
        ranked.commercial_data = {"products": selected, "inventory": inventory}
    return ranked


def _session_product_facts_result(
    state: CommerceConversationState | None,
    resolved_product: Any | None = None,
) -> AgentResult:
    """Ground a talk-first inspect/ack turn in session products, without Tray list search."""
    products: list[dict[str, Any]] = []
    if resolved_product is not None:
        dump = (
            resolved_product.model_dump(mode="json")
            if hasattr(resolved_product, "model_dump")
            else dict(resolved_product)
        )
        products = [dump]
    elif state is not None and getattr(state, "active_product", None) is not None:
        products = [state.active_product.model_dump(mode="json")]
    elif state is not None:
        products = [
            item.model_dump(mode="json")
            for item in (state.last_presented_products or [])[:3]
        ]
    return AgentResult(
        reply_text="",
        intent="commerce",
        handoff_required=False,
        commercial_data={"products": products},
        response_metadata={
            "presented_products": bool(products),
            "used_tray": False,
            "talk_first_skip_catalog_fanout": True,
        },
    )


from .sales.result_utils import mark_sales_result as _mark_sales_result


from .sales.discovery import (  # noqa: E402
    _is_clarification_turn,
    _consecutive_clarification_count,
    _known_preferences,
    _specific_product_lock,
    _subject_identifiable,
    _persona_requires_qualification,
    _preference_key_set,
    _has_urgency_signal,
    build_qualification_snapshot,
    _persona_qualification_question,
    _needs_persona_qualification,
    _comparison_needs_qualification,
    _mentioned_watch_brands,
    _comparison_clarification_question,
    _discovery_state,
    _needs_clarification_before_retrieval,
    is_open_catalog_browse_request,
)


from app.sales.responder import (
    generate_clarification_reply,
    sales_response_with_openai as _sales_response_with_openai,
    responder_contract as _responder_contract,
    deterministic_tray_copy_ready as _deterministic_tray_copy_ready,
)


from .sales.policies.confirmation import (  # noqa: E402
    confirmation_text_kind as _confirmation_text_kind,
)


from .sales.product_lookup import (  # noqa: E402
    execute_compiled_product_retrieval as _execute_compiled_product_retrieval,
    execute_contextual_product_lookup as _execute_contextual_product_lookup,
)
from .sales.checkout_flow import (  # noqa: E402
    _advance_whatsapp_checkout,
    _combine_cart_and_payment_results,
    _combine_checkout_and_followup_results,
    _combine_checkout_channel_result,
    _combine_order_and_payment_results,
    _confirm_current_order_review,
    _create_order_with_payment_lookup,
    _ensure_cart_for_purchase,
    _fulfill_confirmed_order,
    _inspect_listed_products,
    _order_payment_revalidation,
    _pending_action_rejected_result,
    _pending_product_references,
    respond_to_commerce_service as _respond_to_commerce_service,
)


from .sales.policies.action_authority import (  # noqa: E402
    informational_payment_policy_result as _informational_payment_policy_result,
    is_informational_payment_query as _is_informational_payment_query,
    purchase_product_required_result as _purchase_product_required_result,
)
from .sales.catalog_reference import (  # noqa: E402
    resolve_catalog_reference,
)
from .sales.catalog_pending import (  # noqa: E402
    apply_catalog_pending,
)
from .sales.catalog_media import (  # noqa: E402
    try_catalog_media,
    try_remove_cart_item,
)
from .sales.catalog_purchase import (  # noqa: E402
    try_catalog_purchase,
)
from .sales.catalog_retrieve import (  # noqa: E402
    retrieve_catalog_or_clarify,
)



def _hydrate_sales_interpretation(
    semantic_plan: dict[str, Any] | SalesInterpretation | None,
    message: IncomingMessage,
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None = None,
) -> SalesInterpretation | None:
    """Normalize + qualification + memory — one place before the sales handler."""
    if isinstance(semantic_plan, dict):
        semantic_plan = _commerce_plan_to_interpretation(semantic_plan)
    if not isinstance(semantic_plan, SalesInterpretation):
        return None
    from app.catalog.specs.preference_normalize import (
        normalize_sales_interpretation,
        recent_user_context_text,
    )
    from .sales.qualification_slots import (
        apply_stored_qualification_slots,
        continue_commerce_from_qualification_answer,
        rehydrate_qualification_slots_from_turns,
    )
    from app.commerce.commerce_router import is_outbound_catalog_image_request

    open_sale = _open_sale_history(commerce_state)
    from app.sales.search_continuity import reconcile_search_turn
    semantic_plan = reconcile_search_turn(semantic_plan, commerce_state, message.text, recent_turns)
    try:
        semantic_plan = rehydrate_qualification_slots_from_turns(
            semantic_plan,
            recent_turns,
            message_text=message.text,
            conversation_id=message.conversation_id,
            include_other_threads=open_sale,
        )
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("hydrate.qual_rehydrate", exc)
    try:
        semantic_plan = apply_stored_qualification_slots(
            semantic_plan,
            commerce_state,
        )
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("hydrate.qual_state", exc)
    if semantic_plan.references_previous_context and not semantic_plan.domain_change_explicit and commerce_state is not None:
        for field in ("mechanism", "crystal"):
            if getattr(semantic_plan.preferences, field) is None:
                setattr(semantic_plan.preferences, field, (commerce_state.active_preferences or {}).get(field))
    interpretation = normalize_sales_interpretation(
        semantic_plan,
        message_text=message.text,
        context_text=recent_user_context_text(recent_turns),
        recent_turns=recent_turns,
        conversation_id=message.conversation_id,
        include_other_threads=open_sale,
    )
    interpretation = continue_commerce_from_qualification_answer(
        interpretation,
        recent_turns,
        message.text,
        conversation_id=message.conversation_id,
        include_other_threads=open_sale,
        commerce_state=commerce_state,
    )
    interpretation = _rehydrate_contact_preferences(interpretation, message)
    interpretation = reconcile_search_turn(interpretation, commerce_state, message.text, recent_turns)
    if is_outbound_catalog_image_request(message.text):
        interpretation = interpretation.model_copy(update={"image_request": True})
    try:
        from .sales.qualification_slots import store_qualification_slots_on_state

        store_qualification_slots_on_state(commerce_state, interpretation)
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("hydrate.qual_store", exc)
    return interpretation


async def _handle_sales_message_inner(*args, **kwargs):
    from app.agents.commerce import handle_sales_message_inner

    return await handle_sales_message_inner(*args, **kwargs)


async def _handle_sales_catalog_inner(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    *,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    recent_turns: list[dict[str, Any]] | None = None,
) -> AgentResult | None:
    reference = await resolve_catalog_reference(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
    )
    if reference.early_result is not None:
        return reference.early_result
    interpretation = reference.interpretation
    resolved_product = reference.resolved_product
    resolved_by = reference.resolved_by
    purchase_action = interpretation.purchase_action if interpretation is not None else None
    removal = await try_remove_cart_item(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
        purchase_action=purchase_action,
        resolved_product=resolved_product,
    )
    if removal is not None:
        return removal

    pending = await apply_catalog_pending(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
        purchase_action=purchase_action,
        resolved_product=resolved_product,
        resolved_by=resolved_by,
    )
    if pending.early_result is not None:
        return pending.early_result
    interpretation = pending.interpretation
    plan = pending.plan
    purchase_action = pending.purchase_action
    resolved_product = pending.resolved_product
    resolved_by = pending.resolved_by
    purchase_requests = pending.purchase_requests
    unresolved_purchase_items = pending.unresolved_purchase_items
    unresolved_candidates = pending.unresolved_candidates
    pending_link_requested = pending.pending_link_requested

    media = await try_catalog_media(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
        purchase_action=purchase_action,
        resolved_product=resolved_product,
        pending_link_requested=pending_link_requested,
    )
    if media is not None:
        return media

    purchase = await try_catalog_purchase(
        message=message,
        interpretation=interpretation,
        plan=plan,
        state=state,
        purchase_action=purchase_action,
        resolved_product=resolved_product,
        purchase_requests=purchase_requests,
        unresolved_purchase_items=unresolved_purchase_items,
        unresolved_candidates=unresolved_candidates,
    )
    if purchase is not None:
        return purchase

    return await retrieve_catalog_or_clarify(
        message=message,
        facts=facts,
        customer_context=customer_context,
        interpretation=interpretation,
        plan=plan,
        state=state,
        recent_turns=recent_turns,
        resolved_product=resolved_product,
    )


from app.agents.commerce import handle_sales_message as handle_sales_message
