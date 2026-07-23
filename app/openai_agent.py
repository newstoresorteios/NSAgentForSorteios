from __future__ import annotations

import re
import json

from openai import APIError, AsyncOpenAI, OpenAI
from .agent_replies import (
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
from .config import get_settings
from .commerce_context import CommerceConversationState, apply_commerce_domain_context
from .db import load_recent_conversation_turns
from .context_builder import (
    build_template_fallback,
    detect_primary_intent,
    format_facts_for_prompt,
    gather_customer_facts,
)
from .guardrails import (
    detect_available_numbers_inquiry,
    detect_blocked_request,
    default_safe_handoff,
)
from .models import IncomingMessage, AgentResult
from .repository import detect_third_party_account_inquiry, find_coupon_balance_by_phone
from .site_knowledge import HUMAN_SUPPORT_MESSAGE, build_site_knowledge_text, NS_SALES_WHATSAPP
from .vip_profiles import build_vip_openai_context, get_vip_profile, pick_vip_nickname
from .user_preferences import detect_preferred_name_update
from .tray_tools import TOOL_SCHEMAS, execute_tool
from .sales_agent import GREETING_REPLY, OUT_OF_SCOPE_REPLY, deterministic_scope, handle_sales_message, interpret_message


SYSTEM_INSTRUCTIONS = f"""
Você é o NewStoreAgent, atendente virtual da New Store Sorteios.

{build_site_knowledge_text()}

Regras obrigatórias:
- Responda em português do Brasil, de forma curta e clara para WhatsApp.
- Use APENAS os dados consultados no banco e a base oficial acima.
- Nunca invente saldo, cupom, números ou resultados.
- Responda primeiro o que o cliente perguntou; só depois complemente se fizer sentido.
- Nunca consulte ou revele dados de outra pessoa.
- Se o cliente não tiver telefone cadastrado, oriente a acessar https://www.sorteionewstore.com.br/ e incluir o telefone no perfil.
- Não altere cadastro, pagamentos ou participações pelo WhatsApp.
- Não prometa ganhar sorteio; explique regras oficiais.
- Se não souber, oriente o site ou encaminhe para a equipe no WhatsApp {NS_SALES_WHATSAPP}.
- Use a memória do cliente quando disponível; não repita perguntas sobre nome ou preferências já registradas.
- Adapte tom e tamanho da resposta ao estilo preferido do cliente.
- Se a mensagem veio de áudio transcrito, responda naturalmente ao conteúdo falado.
- Para produtos, pre\u00e7os, estoque, clientes e cupons, use as ferramentas de consulta quando dispon\u00edveis.
- Nunca invente pre\u00e7o, estoque, parcelamento ou validade de cupom. `promotional_price` nulo n\u00e3o \u00e9 promo\u00e7\u00e3o.
- Para estoque, considere todos os campos retornados, n\u00e3o apenas `stock > 0`.
- O banco local \u00e9 a fonte oficial para saldo, Cart\u00e3o Presente pessoal, sorteios, participa\u00e7\u00f5es, n\u00fameros e hist\u00f3rico.
- O TrayAdapter \u00e9 a fonte oficial para cat\u00e1logo, produtos, marcas, pre\u00e7os, estoque, EAN, refer\u00eancia e condi\u00e7\u00f5es comerciais.
- Para qualquer informa\u00e7\u00e3o comercial atual, use as tools do TrayAdapter; nunca use exemplos do site como pre\u00e7o ou estoque atual.
- Responda somente sobre a NewStore, seus produtos, compras, atendimento comercial e sorteios; para assuntos externos, use a recusa curta de escopo.
""".strip()

STORE_LOOKUP_UNAVAILABLE = "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."
GENERAL_GREETING_FALLBACK = "Ol\u00e1! Como posso ajudar?"
STORE_KNOWLEDGE_UNAVAILABLE = "Ainda não tenho essa informação oficial da loja disponível neste atendimento."


def _annotate_agent_result(result: AgentResult, **metadata: object) -> AgentResult:
    for key, value in metadata.items():
        if value is not None and key not in result.response_metadata:
            result.response_metadata[key] = value
    return result


def _preferred_name_reply_if_requested(message: IncomingMessage, facts: dict) -> AgentResult | None:
    if not detect_preferred_name_update(message.text):
        return None
    account = facts.get("account") or {}
    if not account.get("found"):
        account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    return build_preferred_name_reply(message, account)


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _sanitize_log_message(text: str) -> str:
    redacted = re.sub(r"sk-(?:proj-)?[^\s'\"]+", "sk-***", text or "")
    return redacted[:300]


def _non_handoff_fallback(message: IncomingMessage, facts: dict) -> str:
    fallback = build_template_fallback(message, facts)
    if fallback:
        return fallback
    if facts.get("primary_intent") == "commerce":
        return STORE_LOOKUP_UNAVAILABLE
    if facts.get("primary_intent") == "general":
        if facts.get("scope_domain") == "store_general":
            return STORE_KNOWLEDGE_UNAVAILABLE
        return GENERAL_GREETING_FALLBACK
    return "N\u00e3o consegui concluir a consulta neste momento. Tente novamente em instantes."


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
    vip_block = ""
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        vip_block = f"\n\n{build_vip_openai_context(vip, nickname)}\n"

    display_name = facts.get("display_name") or customer_context.get("display_name")
    display_label = display_name or message.sender_name or "não informado"
    modality_note = ""
    if message.input_modality == "audio":
        modality_note = "\n- Origem: áudio transcrito para texto"

    return f"""
Mensagem recebida via WhatsApp:
- Nome para tratamento: {display_label}
- Telefone presente: {'sim' if message.sender_phone else 'não'}{modality_note}
- Texto do cliente: {message.text}
- Intenção detectada: {facts.get('primary_intent')}

{format_facts_for_prompt(facts)}
{vip_block}
Responda de forma natural, objetiva e correta.
""".strip()


def generate_openai_reply(
    message: IncomingMessage,
    customer_context: dict,
    facts: dict,
) -> AgentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        return AgentResult(
            reply_text=_non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason="openai_api_key_missing",
        )

    client = OpenAI(api_key=settings.openai_api_key)
    user_input = build_agent_input(message, customer_context, facts)
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": user_input},
            ],
            temperature=0.3,
        )
    except APIError as exc:
        status_code = getattr(exc, "status_code", None)
        print("[openai.agent] request_failed", {
            "status_code": status_code,
            "error_type": type(exc).__name__,
            "model": settings.openai_model,
            "message": _sanitize_log_message(str(exc)),
        })
        return AgentResult(
            reply_text=_non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason=f"openai_error_{status_code or type(exc).__name__}",
        )

    reply = _truncate(
        (response.choices[0].message.content if response.choices else None)
        or _non_handoff_fallback(message, facts),
        settings.max_reply_chars,
    )
    return AgentResult(
        reply_text=reply,
        intent=str(facts.get("primary_intent") or "general_support"),
        handoff_required=False,
    )


def generate_agent_reply(message: IncomingMessage, customer_context: dict) -> AgentResult:
    blocked_reason = detect_blocked_request(message.text)
    if blocked_reason:
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="handoff",
            handoff_required=True,
            safety_reason=blocked_reason,
        )

    scope = deterministic_scope(message.text)
    print("[agent.scope]", {"domain": scope.get("domain")})
    if scope.get("domain") == "out_of_scope":
        return AgentResult(reply_text=OUT_OF_SCOPE_REPLY, intent="out_of_scope", handoff_required=False, safety_reason="scope_refusal")
    if scope.get("domain") == "greeting":
        return AgentResult(reply_text=GREETING_REPLY, intent="general", handoff_required=False)
    primary_intent = detect_primary_intent(message.text)
    print("[agent.route]", {"inbound_id": (message.raw or {}).get("inbound_id"), "primary_intent": primary_intent})
    third_party_reply = _third_party_guardrail(message, primary_intent)
    if third_party_reply:
        return third_party_reply

    if message.input_modality == "audio" and message.transcription_failed:
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui entender agora. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    if message.input_modality == "audio" and not (message.text or "").strip():
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui transcrever. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    facts = gather_customer_facts(message, customer_context)
    facts["scope_domain"] = scope.get("domain")
    preferred_reply = _preferred_name_reply_if_requested(message, facts)
    if preferred_reply:
        return preferred_reply
    local_reply = _local_raffle_reply(message, facts)
    if local_reply:
        return local_reply
    if detect_available_numbers_inquiry(message.text):
        return build_available_numbers_reply(message)
    print("[openai.agent] routing", {
        "mode": "openai_with_db_context",
        "primary_intent": facts.get("primary_intent"),
        "input_modality": message.input_modality,
        "text_preview": (message.text or "")[:160],
        "has_openai_key": bool(get_settings().openai_api_key),
        "transcription_failed": message.transcription_failed,
    })
    return generate_openai_reply(message, customer_context, facts)


async def generate_openai_reply_async(message: IncomingMessage, customer_context: dict, facts: dict) -> AgentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        return generate_openai_reply(message, customer_context, facts)

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_INSTRUCTIONS},
        {"role": "user", "content": build_agent_input(message, customer_context, facts)},
    ]
    tools = (
        TOOL_SCHEMAS
        if facts.get("primary_intent") == "commerce" and settings.tray_adapter_url and settings.tray_adapter_token
        else None
    )
    try:
        for _ in range(3):
            kwargs = {"model": settings.openai_model, "messages": messages, "temperature": 0.3}
            if tools:
                kwargs.update({"tools": tools, "tool_choice": "auto"})
            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0] if response.choices else None
            assistant = choice.message if choice else None
            tool_calls = getattr(assistant, "tool_calls", None) if assistant else None
            if not tool_calls:
                reply = _truncate(
                    (getattr(assistant, "content", None) if assistant else None)
                    or _non_handoff_fallback(message, facts), settings.max_reply_chars,
                )
                return AgentResult(reply_text=reply, intent=str(facts.get("primary_intent") or "general_support"))
            messages.append({"role": "assistant", "content": getattr(assistant, "content", None), "tool_calls": [call.model_dump() for call in tool_calls]})
            for call in tool_calls:
                result = await execute_tool(call.function.name, json.loads(call.function.arguments or "{}"))
                if "error" in result:
                    return AgentResult(reply_text=_non_handoff_fallback(message, facts), intent=str(facts.get("primary_intent") or "store_lookup"), handoff_required=False, safety_reason="tray_adapter_unavailable")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name, "content": json.dumps(result, ensure_ascii=False)})
        return AgentResult(reply_text=_non_handoff_fallback(message, facts), intent=str(facts.get("primary_intent") or "store_lookup"), handoff_required=False, safety_reason="tool_loop_limit")
    except (APIError, json.JSONDecodeError, ValueError) as exc:
        print("[openai.agent] tools_request_failed", {"error_type": type(exc).__name__, "message": _sanitize_log_message(str(exc))})
        return AgentResult(reply_text=_non_handoff_fallback(message, facts), intent=str(facts.get("primary_intent") or "store_lookup"), handoff_required=False, safety_reason="tools_request_failed")


async def generate_agent_reply_async(message: IncomingMessage, customer_context: dict) -> AgentResult:
    blocked_reason = detect_blocked_request(message.text)
    if blocked_reason:
        return _annotate_agent_result(
            AgentResult(reply_text=default_safe_handoff(), intent="handoff", handoff_required=True, safety_reason=blocked_reason),
            domain="guardrail",
            response_source="guardrail",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )

    raw_inbound_id = (message.raw or {}).get("inbound_id")
    try:
        inbound_id = int(raw_inbound_id) if raw_inbound_id is not None else None
    except (TypeError, ValueError):
        inbound_id = None
    recent_turns = load_recent_conversation_turns(
        conversation_id=message.conversation_id,
        sender_phone=message.sender_phone,
        before_inbound_id=inbound_id,
        limit=8,
    )
    context_source = "conversation_id" if message.conversation_id else ("sender_phone" if message.sender_phone else "none")
    print("[sales.context]", {
        "history_turns": len(recent_turns),
        "history_user_turns": sum(1 for turn in recent_turns if turn.get("role") == "user"),
        "history_assistant_turns": sum(1 for turn in recent_turns if turn.get("role") == "assistant"),
        "conversation_id_present": bool(message.conversation_id),
        "before_inbound_id_present": inbound_id is not None,
        "context_source": context_source,
    })
    commerce_state = CommerceConversationState.from_payload(
        customer_context.get("_commerce_state")
    )
    interpretation = await interpret_message(
        message,
        recent_turns=recent_turns,
        commerce_state=commerce_state,
    )
    used_openai_interpreter = interpretation._source == "openai"
    interpreted_domain = interpretation.domain
    interpretation, domain_context_applied = apply_commerce_domain_context(
        interpretation,
        commerce_state,
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
    raffle_intents = {"balance", "coupon_code", "simulation", "raffle_history", "current_raffle", "rules"}
    scope_domain = (
        "raffle"
        if not used_openai_interpreter and primary_intent in raffle_intents
        else interpretation.domain
    )
    print("[agent.scope]", {"domain": scope_domain})
    if scope_domain == "out_of_scope":
        return _annotate_agent_result(
            AgentResult(reply_text=OUT_OF_SCOPE_REPLY, intent="out_of_scope", handoff_required=False, safety_reason="scope_refusal"),
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="guardrail" if used_openai_interpreter else "deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=interpretation._fallback_reason,
        )
    if scope_domain == "greeting":
        return _annotate_agent_result(
            AgentResult(reply_text=GREETING_REPLY, intent="general", handoff_required=False),
            domain="greeting",
            response_source="local_greeting",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=interpretation._fallback_reason,
        )
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
    if message.input_modality == "audio" and (message.transcription_failed or not (message.text or "").strip()):
        return _annotate_agent_result(
            generate_agent_reply(message, customer_context),
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="technical_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="audio_transcription_failed",
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
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
        if commerce_result is not None:
            return _annotate_agent_result(
                commerce_result,
                domain="commerce",
                goal=interpretation.goal,
                used_openai_interpreter=used_openai_interpreter,
                fallback_reason=interpretation._fallback_reason,
            )
    print("[openai.agent] routing", {"mode": "openai_with_db_context_and_tools", "primary_intent": facts.get("primary_intent"), "has_openai_key": bool(get_settings().openai_api_key), "tray_tools_enabled": bool(get_settings().tray_adapter_url and get_settings().tray_adapter_token)})
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
    )
