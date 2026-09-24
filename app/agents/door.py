from __future__ import annotations

from app.configuration.runtime import message as operator_message

import re
import json

from openai import APIError
from app.llm.agent_replies import (
    build_available_numbers_reply,
    build_balance_reply,
    build_coupon_code_reply,
    build_current_raffle_reply,
    build_raffle_history_reply,
    build_rules_reply_result,
    build_simulation_reply,
    build_preferred_name_reply,
    _third_party_reply,
)
from app.config import get_settings
from app.commerce.commerce_context import CommerceConversationState, apply_commerce_domain_context
from app.db import load_recent_conversation_turns
from app.catalog.vision.image_product_id import handle_image_product_search, image_search_eligible
from app.memory.context_builder import (
    build_template_fallback,
    detect_primary_intent,
    format_facts_for_prompt,
    gather_customer_facts,
)
from app.verify.guardrails import (
    detect_available_numbers_inquiry,
    detect_blocked_request,
    detect_explicit_raffle_intent,
    default_safe_handoff,
)
from app.ops.handoff_service import build_human_handoff_result, should_request_human_handoff
from app.models import IncomingMessage, AgentResult
from app.ops.turn_runtime import LLMCallBudgetExceeded
from app.memory.context_resume import (
    build_contextual_greeting,
    build_pending_payment_resume_result,
    build_presented_catalog_resume_result,
    has_resumable_commerce,
    is_payment_link_request,
    is_soft_greeting,
    is_unpaid_order_resume_request,
    should_redisplay_presented_catalog,
    should_resume_pending_order,
)
from app.commerce.order_context_recovery import (
    extract_handles_from_conversation,
    hydrate_state_from_handles,
    recover_order_id_from_customer,
)
from app.commerce.order_service import (
    contains_tax_document_candidate,
    extract_order_reference,
    extract_valid_tax_document,
    find_order_by_customer_document,
    get_order_facts,
    invalid_tax_document_result,
    is_order_lookup_request,
    is_order_notes_request,
    order_notes_unavailable_result,
)
from app.commerce.payment_service import inspect_order_payment
from app.identity.repository import detect_third_party_account_inquiry, find_coupon_balance_by_phone
from app.persona.site_knowledge import HUMAN_SUPPORT_MESSAGE, build_site_knowledge_text, NS_SALES_WHATSAPP
from app.identity.user_preferences import detect_preferred_name_update
from app.tray.tray_tools import execute_tool
from app.sales_agent import (
    OUT_OF_SCOPE_REPLY,
    deterministic_scope,
    handle_sales_message,
    interpret_message,
)
from app.identity.greeting_policy import (
    GREETING_REPLY,
    choose_farewell_reply,
    choose_greeting_reply,
    is_farewell_message,
    is_greeting_message as _is_greeting,
    resolve_address_name,
    sanitize_greeting_reply,
)


def PERSONA_GREETING_OPERATIONAL():
    return operator_message('business.persona_greeting_operational.02a8ba3200').strip()


def SYSTEM_INSTRUCTIONS():
    return operator_message('business.system_instructions.a369e54ee8', value_1=f'{build_site_knowledge_text()}', NS_SALES_WHATSAPP=f'{NS_SALES_WHATSAPP()}').strip()

def STORE_LOOKUP_UNAVAILABLE():
    return operator_message('business.store_lookup_unavailable.08732f3104')
def GENERAL_GREETING_FALLBACK():
    return operator_message('business.general_greeting_fallback.e280997ce6')
def STORE_KNOWLEDGE_UNAVAILABLE():
    return operator_message('business.store_knowledge_unavailable.81b8d95141')


def _annotate_agent_result(result: AgentResult, **metadata: object) -> AgentResult:
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None and "persona_runtime" not in result.response_metadata:
            result.response_metadata["persona_runtime"] = runtime.flow_params_dict()
    except Exception:
        pass
    for key, value in metadata.items():
        if value is not None and key not in result.response_metadata:
            result.response_metadata[key] = value
    # Phase 8: count skipped LLM slots when deterministic / partial paths win.
    if "used_openai_interpreter" in metadata or "used_openai_responder" in metadata:
        from app.ops.runtime_context import register_avoided_llm_call

        used_interpreter = bool(
            result.response_metadata.get("used_openai_interpreter")
        )
        used_responder = bool(result.response_metadata.get("used_openai_responder"))
        source = str(
            result.response_metadata.get("response_source") or "deterministic"
        )
        skipped: list[str] = []
        if not used_interpreter:
            skipped.append("decision")
        if not used_responder:
            skipped.append("response_composition")
        if skipped:
            register_avoided_llm_call(
                f"path:{source}",
                intended_call_types=skipped,
            )
    return result


def _preferred_name_reply_if_requested(message: IncomingMessage, facts: dict) -> AgentResult | None:
    if not detect_preferred_name_update(message.text):
        return None
    account = facts.get("account") or {}
    if not account.get("found"):
        account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    return build_preferred_name_reply(message, account)


def _truncate(text: str, max_chars: int) -> str:
    from app.llm.response_composer import truncate_reply
    return truncate_reply(text, max_chars)


def _sanitize_log_message(text: str) -> str:
    redacted = re.sub(r"sk-(?:proj-)?[^\s'\"]+", "sk-***", text or "")
    return redacted[:300]


def _non_handoff_fallback(message: IncomingMessage, facts: dict) -> str:
    fallback = build_template_fallback(message, facts)
    if fallback:
        return fallback
    if facts.get("primary_intent") == "commerce":
        return STORE_LOOKUP_UNAVAILABLE()
    if facts.get("primary_intent") == "general":
        if facts.get("scope_domain") == "store_general":
            return STORE_KNOWLEDGE_UNAVAILABLE()
        return GENERAL_GREETING_FALLBACK()
    return operator_message('agents.door._non_handoff_fallback.f9f5712adb')


def _is_personal_intent(intent: str) -> bool:
    return intent in {"balance", "coupon_code", "raffle_history", "simulation"}


def _third_party_guardrail(message: IncomingMessage, primary_intent: str) -> AgentResult | None:
    if _is_personal_intent(primary_intent) and detect_third_party_account_inquiry(message.text, message.sender_phone):
        return _third_party_reply()
    return None


def _local_raffle_reply(message: IncomingMessage, facts: dict) -> AgentResult | None:
    handlers = {
        "balance": build_balance_reply,
        "coupon_code": build_coupon_code_reply,
        "simulation": build_simulation_reply,
        "raffle_history": build_raffle_history_reply,
        "current_raffle": build_current_raffle_reply,
        "rules": build_rules_reply_result,
    }
    handler = handlers.get(str(facts.get("primary_intent")))
    if handler:
        print("[raffle.route]", {"intent": facts.get("primary_intent")})
    return handler(message) if handler else None


def build_agent_input(message: IncomingMessage, customer_context: dict, facts: dict) -> str:
    from app.memory.working_memory import format_working_memory_block

    checkout_name = None
    commerce_state = (
        customer_context.get("_commerce_state")
        if isinstance(customer_context, dict)
        else None
    )
    if isinstance(commerce_state, dict):
        draft = commerce_state.get("checkout_draft") or {}
        customer = draft.get("customer") if isinstance(draft, dict) else None
        if isinstance(customer, dict):
            checkout_name = customer.get("name")
    trusted_account_name = None
    if isinstance(customer_context, dict) and customer_context.get("found"):
        trusted_account_name = facts.get("display_name") or customer_context.get("display_name")
    display_name = resolve_address_name(
        preferred_name=(customer_context or {}).get("preferred_name")
        if isinstance(customer_context, dict)
        else None,
        checkout_name=checkout_name,
        account_name=trusted_account_name,
        whatsapp_profile_name=message.sender_name,
    )
    display_label = display_name or (
        'não informado; fale sem vocativo ou use "cliente" de forma cordial, '
        "nunca use o nome bruto do perfil"
    )
    modality_note = ""
    if message.input_modality == "audio":
        modality_note = "\n- Origem: áudio transcrito para texto"

    channel_label = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "whatsapp": "WhatsApp",
        "widget": "chat do site",
    }.get(message.channel, message.channel or "canal não identificado")
    working_memory_block = format_working_memory_block(
        customer_context.get("_commerce_state")
    )
    memory_section = f"\n\n{working_memory_block}" if working_memory_block else ""
    return f"""
Mensagem recebida via {channel_label}:
- Nome para tratamento: {display_label}
- Telefone presente: {'sim' if message.sender_phone else 'não'}{modality_note}
- Texto do cliente: {message.text}
- Intenção detectada: {facts.get('primary_intent')}
{memory_section}

{format_facts_for_prompt(facts)}
Responda de forma natural, objetiva e correta.
Use WORKING_MEMORY só internamente; não ofereça pedido/link/dados sem o cliente pedir.
""".strip()


async def generate_persona_greeting_reply(
    message: IncomingMessage,
    customer_context: dict,
    *,
    recent_turns: list[dict] | None = None,
    conversation_state: CommerceConversationState | None = None,
) -> AgentResult:
    """Answer pure greetings with the compiled DB persona (Crono) as authority."""
    settings = get_settings()
    if not (
        bool(getattr(settings, "agent_db_persona_enabled", False))
        and settings.openai_api_key
    ):
        return AgentResult(
            reply_text=choose_greeting_reply(recent_turns),
            intent="general",
            handoff_required=False,
            safety_reason="persona_greeting_unavailable",
        )

    from app.llm.prompt_compiler import resolve_system_instructions

    facts = gather_customer_facts(message, customer_context)
    facts["scope_domain"] = "greeting"
    facts["primary_intent"] = "greeting"
    facts["intents"] = [*facts.get("intents", []), "greeting"]
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None:
            if runtime.greeting_text:
                facts["official_greeting"] = runtime.greeting_text
            if runtime.agent_display_name:
                facts["agent_display_name"] = runtime.agent_display_name
            if runtime.tone:
                facts["persona_tone"] = runtime.tone
    except Exception:
        pass
    system_instructions = resolve_system_instructions(
        fallback_instructions=PERSONA_GREETING_OPERATIONAL(),
        incoming=message,
        conversation_state=conversation_state,
        recent_turns=recent_turns,
    )
    messages = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": build_agent_input(message, customer_context, facts)},
    ]
    try:
        from app.llm.openai_errors import OpenAIGatewayError
        from app.llm.openai_gateway import generate_text_output

        text_result = await generate_text_output(
            model=settings.openai_model,
            messages=messages,
            temperature=0.4,
            call_type="persona_greeting",
        )
        content = (text_result.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        print("[openai.agent.persona_greeting.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return AgentResult(
            reply_text=choose_greeting_reply(recent_turns),
            intent="general",
            handoff_required=False,
            safety_reason=f"persona_greeting_error:{type(exc).__name__}",
        )
    if not content:
        return AgentResult(
            reply_text=choose_greeting_reply(recent_turns),
            intent="general",
            handoff_required=False,
            safety_reason="persona_greeting_empty",
        )
    content = sanitize_greeting_reply(content)
    if not content or content.casefold().startswith("saudação padrão"):
        content = choose_greeting_reply(recent_turns)
    from app.persona.persona_runtime import runtime_setting

    return AgentResult(
        reply_text=_truncate(
            content,
            int(runtime_setting("maxReplyChars", settings.max_reply_chars)),
        ),
        intent="general",
        handoff_required=False,
    )


from app.agents.door_legacy import generate_agent_reply, generate_openai_reply


async def generate_openai_reply_async(message: IncomingMessage, customer_context: dict, facts: dict) -> AgentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        return generate_openai_reply(message, customer_context, facts)

    from app.llm.prompt_compiler import legacy_contract_extra_blocks, resolve_system_instructions

    # Non-commerce door path is text-only. Commerce must go through handle_sales_message
    # / ProductRetrievalCompiler — never open TOOL_SCHEMAS here.
    system_instructions = resolve_system_instructions(
        fallback_instructions=SYSTEM_INSTRUCTIONS(),
        incoming=message,
        extra_system_blocks=[
            *legacy_contract_extra_blocks(
                SYSTEM_INSTRUCTIONS(),
                tag="legacy_agent_contract",
            ),
            (
                operator_message('agents.door.generate_openai_reply_async.d5937d8b8d')
            ),
        ],
    )
    messages: list[dict] = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": build_agent_input(message, customer_context, facts)},
    ]
    try:
        from app.llm.openai_errors import OpenAIGatewayError
        from app.llm.openai_gateway import generate_text_output

        text_result = await generate_text_output(
            model=settings.openai_model,
            messages=messages,
            temperature=0.3,
            call_type="response_composition",
        )
        from app.persona.persona_runtime import runtime_setting

        reply = _truncate(
            text_result.text or _non_handoff_fallback(message, facts),
            int(runtime_setting("maxReplyChars", settings.max_reply_chars)),
        )
        return AgentResult(
            reply_text=reply,
            intent=str(facts.get("primary_intent") or "general_support"),
        )
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print("[openai.agent] text_request_failed", {"error_type": type(exc).__name__, "message": _sanitize_log_message(str(exc))})
        return AgentResult(reply_text=_non_handoff_fallback(message, facts), intent=str(facts.get("primary_intent") or "store_lookup"), handoff_required=False, safety_reason="tools_request_failed")


async def _resolve_greeting_door(
    *,
    message: IncomingMessage,
    customer_context: dict,
    recent_turns: list[dict] | None,
    commerce_state,
    phrase_restart: bool,
    used_openai_interpreter: bool = False,
    fallback_reason: str | None = None,
    interpretation_goal=None,
    allow_payment_resume: bool = True,
    allow_persona_greeting: bool = True,
) -> AgentResult | None:
    """Single greeting/resume door. Never opens sales handle or the tool loop."""
    from app.sales.dialogue_phase import blocks_greeting_fast_path

    if blocks_greeting_fast_path(commerce_state):
        return None
    if allow_payment_resume:
        payment_resume = build_pending_payment_resume_result(commerce_state)
        if (
            payment_resume is not None
            and not phrase_restart
            and should_resume_pending_order(
                message.text,
                commerce_state,
                is_greeting=True,
            )
        ):
            return _annotate_agent_result(
                payment_resume,
                domain="commerce",
                response_source="context_resume_payment_url",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=fallback_reason,
            )
    if has_resumable_commerce(commerce_state):
        resume = build_contextual_greeting(
            commerce_state,
            recent_turns=recent_turns,
        )
        return _annotate_agent_result(
            resume,
            domain=resume.response_metadata.get("domain") or "commerce",
            response_source=resume.response_metadata.get(
                "response_source",
                "context_resume",
            ),
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=fallback_reason,
        )
    if not allow_persona_greeting:
        return None
    settings = get_settings()
    from app.persona.persona_runtime import get_persona_runtime

    runtime = get_persona_runtime()
    greeting_mode = (
        runtime.greeting_mode
        if runtime is not None
        else "persona_text"
    )
    has_official_greeting = bool(
        runtime is not None and (runtime.greeting_text or "").strip()
    )
    if has_official_greeting and greeting_mode != "persona_llm":
        return _annotate_agent_result(
            AgentResult(
                reply_text=choose_greeting_reply(recent_turns),
                intent="general",
                handoff_required=False,
            ),
            domain="greeting",
            response_source="persona_greeting",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=fallback_reason,
        )
    if (
        greeting_mode == "persona_llm"
        and bool(getattr(settings, "agent_db_persona_enabled", False))
        and settings.openai_api_key
    ):
        persona_reply = await generate_persona_greeting_reply(
            message,
            customer_context,
            recent_turns=recent_turns,
            conversation_state=commerce_state,
        )
        if persona_reply and not persona_reply.safety_reason:
            cleaned = sanitize_greeting_reply(persona_reply.reply_text)
            if cleaned and "saudação padrão" not in cleaned.casefold():
                persona_reply.reply_text = cleaned
                return _annotate_agent_result(
                    persona_reply,
                    domain="greeting",
                    goal=interpretation_goal,
                    response_source="openai",
                    used_openai_interpreter=used_openai_interpreter,
                    used_openai_responder=True,
                    used_tray=False,
                    fallback_reason=fallback_reason,
                )
    return _annotate_agent_result(
        AgentResult(
            reply_text=choose_greeting_reply(recent_turns),
            intent="general",
            handoff_required=False,
        ),
        domain="greeting",
        response_source="persona_greeting" if has_official_greeting else "local_greeting",
        used_openai_interpreter=used_openai_interpreter if has_official_greeting else False,
        used_openai_responder=False,
        used_tray=False,
        fallback_reason=fallback_reason,
    )


async def generate_agent_reply_async(message: IncomingMessage, customer_context: dict) -> AgentResult:
    from app.persona.persona_runtime import (
        get_persona_runtime,
        load_persona_runtime,
        reset_persona_runtime,
        set_persona_runtime,
    )

    if get_persona_runtime() is not None:
        return await _generate_agent_reply_async_inner(message, customer_context)

    persona_runtime = load_persona_runtime()
    persona_token = set_persona_runtime(persona_runtime)
    customer_context["_persona_runtime"] = persona_runtime.flow_params_dict()
    try:
        return await _generate_agent_reply_async_inner(message, customer_context)
    finally:
        reset_persona_runtime(persona_token)


async def _generate_agent_reply_async_inner(
    message: IncomingMessage,
    customer_context: dict,
) -> AgentResult:
    from app.agents.door_gates import (
        try_accepted_handoff,
        try_entry_gates,
        try_farewell,
    )
    from app.agents.door_media import try_media_routes
    from app.agents.door_order import try_order_resume_route, try_tax_document_route

    early = try_entry_gates(message)
    if early is not None:
        return early

    from app.persona.institutional_route import answer_institutional
    institutional = await answer_institutional(message)
    if institutional is not None:
        return _annotate_agent_result(institutional)

    raw_inbound_id = (message.raw or {}).get("inbound_id")
    try:
        inbound_id = int(raw_inbound_id) if raw_inbound_id is not None else None
    except (TypeError, ValueError):
        inbound_id = None
    settings = get_settings()
    from app.memory.history_window import (
        count_user_assistant_turns,
        resolve_history_hard_cap,
        resolve_model_history_limit,
        select_model_history_turns,
        turns_for_conversation,
    )

    history_limit = resolve_model_history_limit(settings)
    history_hard_cap = resolve_history_hard_cap(settings)
    commerce_state = CommerceConversationState.from_payload(
        customer_context.get("_commerce_state")
    )
    history_lookup = {
        "conversation_id": message.conversation_id,
        "sender_phone": message.sender_phone,
        "before_inbound_id": inbound_id,
        "limit": history_hard_cap,
        "sender_key": message.sender_key,
        "hard_cap": history_hard_cap,
        "after_inbound_id": commerce_state.history_cut_inbound_id,
    }
    recovery_turns = load_recent_conversation_turns(**history_lookup)
    from app.sales.dialogue_phase import is_open_sale_state

    thread_turns = turns_for_conversation(
        recovery_turns,
        message.conversation_id,
        include_other_threads=is_open_sale_state(commerce_state),
    )
    model_turns = select_model_history_turns(thread_turns, limit=history_limit)
    recent_turns = model_turns
    context_source = (
        "conversation_id"
        if message.conversation_id
        else ("sender_key" if message.sender_key else ("sender_phone" if message.sender_phone else "none"))
    )
    from app.ops.observability import (
        log_event,
        redact_text,
        summarize_commerce_state,
        summarize_history_turns,
    )

    recovery_counts = count_user_assistant_turns(recovery_turns)
    model_counts = count_user_assistant_turns(model_turns)
    log_event(
        "sales.context",
        {
            "history_turns": model_counts["total"],
            "recovery_turns": recovery_counts["total"],
            "history_limit": history_limit,
            "history_hard_cap": history_hard_cap,
            "history_user_turns": model_counts["user"],
            "history_assistant_turns": model_counts["assistant"],
            "conversation_id_present": bool(message.conversation_id),
            "sender_key_present": bool(message.sender_key),
            "before_inbound_id_present": inbound_id is not None,
            "context_source": context_source,
        },
    )
    customer_context["_conversation_turns"] = recovery_turns
    customer_context["_model_conversation_turns"] = model_turns
    accepted = try_accepted_handoff(message, recovery_turns)
    if accepted is not None:
        return accepted
    log_event(
        "history.loaded",
        {
            "context_source": context_source,
            "history_turns": model_counts["total"],
            "recovery_turns": recovery_counts["total"],
            "history_limit": history_limit,
            "history_hard_cap": history_hard_cap,
            "history_preview": summarize_history_turns(model_turns),
            "recovery_preview": summarize_history_turns(recovery_turns),
            "commerce_state": summarize_commerce_state(commerce_state),
            "inbound_text_preview": redact_text(message.text, max_chars=500),
            "channel": message.channel,
        },
    )
    tax = await try_tax_document_route(message, commerce_state)
    if tax is not None:
        return tax
    farewell = try_farewell(message, customer_context, commerce_state)
    if farewell is not None:
        return farewell
    from app.sales.qualification_slots import (
        extract_introduced_name,
        is_qualification_slot_answer,
    )
    from app.sales.dialogue_phase import (
        blocks_greeting_fast_path,
        is_fresh_commerce_start,
        is_open_sale_state,
        reset_browse_memory_keep_orders,
        should_reset_browse_memory,
    )

    answering_qualification = is_qualification_slot_answer(
        recovery_turns or recent_turns,
        message.text,
        conversation_id=message.conversation_id,
        include_other_threads=is_open_sale_state(commerce_state),
    ) or bool(extract_introduced_name(message.text))
    soft_greeting = (not answering_qualification) and (
        _is_greeting(message.text) or is_soft_greeting(message.text)
    )
    context_handles = extract_handles_from_conversation(
        state=commerce_state,
        recent_turns=recovery_turns or recent_turns,
        message_text=message.text,
    )
    commerce_state = hydrate_state_from_handles(commerce_state, context_handles)
    already_reset = bool(
        isinstance(customer_context, dict)
        and customer_context.get("_browse_reset_this_turn")
    )
    fresh_start = should_reset_browse_memory(
        message.text,
        conversation_id=message.conversation_id,
        state=commerce_state,
    )
    if already_reset or fresh_start:
        if (not already_reset) and fresh_start:
            commerce_state = reset_browse_memory_keep_orders(commerce_state)
            if inbound_id is not None:
                commerce_state.history_cut_inbound_id = inbound_id
        model_turns = []
        recent_turns = []
        customer_context["_model_conversation_turns"] = []
    customer_context["_commerce_state"] = commerce_state.model_dump(mode="json")
    phrase_restart = is_fresh_commerce_start(message.text)
    from app.sales.dialogue_phase import is_bare_commerce_restart
    if is_bare_commerce_restart(message.text):
        return AgentResult(
            reply_text=operator_message("commerce_search_restarted"),
            intent="commerce",
            response_metadata={"response_source": "commerce_search_restart",
                               "commerce_state": commerce_state.model_dump(mode="json"),
                               "browse_reset": True},
        )
    resume_pending_order_early = (not phrase_restart) and should_resume_pending_order(
        message.text,
        commerce_state,
        is_greeting=soft_greeting,
        allow_without_state=bool(
            context_handles.get("order_ids")
            or context_handles.get("payment_urls")
            or context_handles.get("documents")
            or context_handles.get("emails")
        ),
    )
    if (
        soft_greeting
        and has_resumable_commerce(commerce_state)
        and not blocks_greeting_fast_path(commerce_state)
        and not resume_pending_order_early
        and not is_order_lookup_request(message.text, commerce_state=commerce_state)
        and not is_unpaid_order_resume_request(message.text)
        and not is_payment_link_request(message.text)
    ):
        greeting = await _resolve_greeting_door(
            message=message,
            customer_context=customer_context,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
            phrase_restart=phrase_restart,
            allow_payment_resume=False,
            allow_persona_greeting=False,
        )
        if greeting is not None:
            return greeting
    order = await try_order_resume_route(
        message=message,
        commerce_state=commerce_state,
        context_handles=context_handles,
        fresh_start=fresh_start,
        soft_greeting=soft_greeting,
        resume_pending_order_early=resume_pending_order_early,
        order_reference=extract_order_reference(message.text),
    )
    if order is not None:
        return order
    media = await try_media_routes(message, commerce_state)
    if media is not None:
        return media
    return await _route_after_interpret(
        message=message,
        customer_context=customer_context,
        commerce_state=commerce_state,
        recovery_turns=recovery_turns,
        recent_turns=recent_turns,
        model_turns=model_turns,
        answering_qualification=answering_qualification,
        phrase_restart=phrase_restart,
    )


async def _route_after_interpret(
    *,
    message: IncomingMessage,
    customer_context: dict,
    commerce_state,
    recovery_turns,
    recent_turns,
    model_turns,
    answering_qualification: bool,
    phrase_restart: bool,
) -> AgentResult:
    interpretation = await interpret_message(
        message,
        recent_turns=model_turns,
        commerce_state=commerce_state,
    )
    from app.sales.qualification_slots import continue_commerce_from_qualification_answer
    from app.sales.dialogue_phase import blocks_greeting_fast_path

    interpretation = continue_commerce_from_qualification_answer(
        interpretation,
        recovery_turns or recent_turns,
        message.text,
        conversation_id=message.conversation_id,
        commerce_state=commerce_state,
    )
    used_openai_interpreter = interpretation._source == "openai"
    interpreted_domain = interpretation.domain
    interpretation, domain_context_applied = apply_commerce_domain_context(
        interpretation,
        commerce_state,
        message_text=message.text,
    )
    print("[sales.domain.context]", {
        "previous_domain": commerce_state.active_domain,
        "interpreted_domain": interpreted_domain,
        "domain_changed": bool(
            commerce_state.active_domain
            and commerce_state.active_domain != interpretation.domain
        ),
        "change_explicit": interpretation.domain_change_explicit,
        "context_override": domain_context_applied,
    })
    primary_intent = detect_primary_intent(message.text)
    scope_domain = (
        "raffle"
        if detect_explicit_raffle_intent(message.text)
        else interpretation.domain
    )
    from app.sales.conversation_repair import is_conversation_repair
    if scope_domain != "raffle" and is_conversation_repair(message.text, interpretation):
        scope_domain = "commerce"
    print("[agent.scope]", {"domain": scope_domain})
    if scope_domain == "out_of_scope":
        return _annotate_agent_result(
            AgentResult(reply_text=OUT_OF_SCOPE_REPLY(), intent="out_of_scope", handoff_required=False, safety_reason="scope_refusal"),
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="guardrail" if used_openai_interpreter else "deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=interpretation._fallback_reason,
        )
    if (not answering_qualification) and (
        not blocks_greeting_fast_path(commerce_state)
    ) and (
        scope_domain == "greeting"
        or (
            interpretation._source != "openai"
            and is_soft_greeting(message.text)
            and has_resumable_commerce(commerce_state)
        )
    ):
        greeting = await _resolve_greeting_door(
            message=message,
            customer_context=customer_context,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
            phrase_restart=phrase_restart,
            used_openai_interpreter=used_openai_interpreter,
            fallback_reason=interpretation._fallback_reason,
            interpretation_goal=interpretation.goal,
        )
        if greeting is not None:
            return greeting
    print("[agent.route]", {"inbound_id": (message.raw or {}).get("inbound_id"), "primary_intent": primary_intent})
    third_party_reply = _third_party_guardrail(message, primary_intent)
    if third_party_reply:
        return _annotate_agent_result(
            third_party_reply,
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="guardrail",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
        )
    facts = gather_customer_facts(message, customer_context)
    facts["scope_domain"] = scope_domain
    if scope_domain == "commerce":
        facts = {**facts, "primary_intent": "commerce", "intents": [*facts.get("intents", []), "commerce"]}
    preferred_reply = _preferred_name_reply_if_requested(message, facts)
    if preferred_reply:
        return _annotate_agent_result(
            preferred_reply,
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
        )
    if scope_domain == "raffle":
        local_reply = _local_raffle_reply(message, facts)
        if local_reply:
            return _annotate_agent_result(
                local_reply,
                domain="raffle",
                goal=interpretation.goal,
                response_source="local_raffle",
                used_openai_interpreter=used_openai_interpreter,
                used_openai_responder=False,
                used_tray=False,
            )
        if detect_available_numbers_inquiry(message.text):
            return _annotate_agent_result(
                build_available_numbers_reply(message),
                domain="raffle",
                goal=interpretation.goal,
                response_source="local_raffle",
                used_openai_interpreter=used_openai_interpreter,
                used_openai_responder=False,
                used_tray=False,
            )
    if scope_domain == "commerce":
        commerce_result = await handle_sales_message(
            message,
            facts,
            customer_context,
            interpretation,
            recent_turns=model_turns,
            commerce_state=commerce_state,
        )
        if commerce_result is not None:
            return _annotate_agent_result(
                commerce_result,
                domain="commerce",
                goal=interpretation.goal,
                used_openai_interpreter=used_openai_interpreter,
                fallback_reason=interpretation._fallback_reason,
                interpretation_confidence=interpretation.confidence,
            )
        return _annotate_agent_result(
            AgentResult(
                reply_text=(
                    operator_message('agents.door._route_after_interpret.8054ad3cc3')
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            domain="commerce",
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="commerce_handle_empty",
            interpretation_confidence=interpretation.confidence,
        )
    print("[openai.agent] routing", {"mode": "openai_text_only", "primary_intent": facts.get("primary_intent"), "has_openai_key": bool(get_settings().openai_api_key), "tray_tools_enabled": False})
    result = await generate_openai_reply_async(message, customer_context, facts)
    return _annotate_agent_result(
        result,
        domain=scope_domain,
        goal=interpretation.goal,
        response_source="technical_fallback" if result.safety_reason else "openai",
        used_openai_interpreter=used_openai_interpreter,
        used_openai_responder=not bool(result.safety_reason),
        used_tray=False,
        fallback_reason=result.safety_reason or interpretation._fallback_reason,
        interpretation_confidence=interpretation.confidence,
    )


