from __future__ import annotations

import re

from openai import APIStatusError, OpenAI
from .agent_replies import (
    build_available_numbers_reply,
    build_preferred_name_reply,
    _third_party_reply,
)
from .config import get_settings
from .context_builder import (
    build_template_fallback,
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
""".strip()


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _sanitize_log_message(text: str) -> str:
    redacted = re.sub(r"sk-(?:proj-)?[^\s'\"]+", "sk-***", text or "")
    return redacted[:300]


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
        fallback = build_template_fallback(message, facts)
        return AgentResult(
            reply_text=fallback or default_safe_handoff(),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=fallback is None,
            safety_reason=None if fallback else "openai_api_key_missing",
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
    except APIStatusError as exc:
        print("[openai.agent] request_failed", {
            "status_code": exc.status_code,
            "error_type": type(exc).__name__,
            "model": settings.openai_model,
            "message": _sanitize_log_message(str(exc)),
        })
        fallback = build_template_fallback(message, facts) or default_safe_handoff()
        return AgentResult(
            reply_text=fallback,
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=exc.status_code == 401,
            safety_reason=f"openai_error_{exc.status_code}",
        )

    reply = _truncate(
        (response.choices[0].message.content if response.choices else None)
        or build_template_fallback(message, facts)
        or default_safe_handoff(),
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

    if detect_available_numbers_inquiry(message.text):
        return build_available_numbers_reply(message)

    if detect_third_party_account_inquiry(message.text, message.sender_phone):
        return _third_party_reply()

    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    preferred_reply = build_preferred_name_reply(message, account)
    if preferred_reply:
        return preferred_reply

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
    print("[openai.agent] routing", {
        "mode": "openai_with_db_context",
        "primary_intent": facts.get("primary_intent"),
        "input_modality": message.input_modality,
        "text_preview": (message.text or "")[:160],
        "has_openai_key": bool(get_settings().openai_api_key),
        "transcription_failed": message.transcription_failed,
    })
    return generate_openai_reply(message, customer_context, facts)
