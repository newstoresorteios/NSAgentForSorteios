from __future__ import annotations

import re

from openai import APIStatusError, OpenAI
from .agent_replies import (
    build_available_numbers_reply,
    build_balance_reply,
    build_coupon_code_reply,
    build_current_raffle_reply,
    build_preferred_name_reply,
    build_raffle_history_reply,
    build_rules_reply_result,
    build_simulation_reply,
    _third_party_reply,
)
from .repository import find_coupon_balance_by_phone
from .config import get_settings
from .guardrails import (
    detect_available_numbers_inquiry,
    detect_balance_inquiry,
    detect_blocked_request,
    detect_coupon_code_inquiry,
    detect_current_raffle_inquiry,
    detect_human_support_request,
    detect_raffle_history_inquiry,
    detect_rules_inquiry,
    detect_simulation_inquiry,
    default_safe_handoff,
)
from .models import IncomingMessage, AgentResult
from .repository import detect_third_party_account_inquiry
from .site_knowledge import HUMAN_SUPPORT_MESSAGE, build_site_knowledge_text, NS_SALES_WHATSAPP
from .vip_profiles import build_vip_openai_context, get_vip_profile, pick_vip_nickname


SYSTEM_INSTRUCTIONS = f"""
Você é o NewStoreAgent, atendente virtual da New Store Sorteios.

{build_site_knowledge_text()}

Regras obrigatórias:
- Responda em português do Brasil, de forma curta e clara.
- Use apenas as informações acima e o contexto recebido.
- Nunca consulte ou revele dados de outra pessoa.
- Se o cliente não tiver telefone cadastrado, oriente a acessar https://www.sorteionewstore.com.br/ e incluir o telefone no perfil.
- Não altere cadastro, pagamentos ou participações pelo WhatsApp.
- Não prometa ganhar sorteio; explique regras oficiais.
- Se não souber, oriente o site ou encaminhe para a equipe no WhatsApp {NS_SALES_WHATSAPP} (vendas e dúvidas).
- Se o cliente quiser falar com um atendente humano, informe o contato {NS_SALES_WHATSAPP}.
""".strip()


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _sanitize_log_message(text: str) -> str:
    redacted = re.sub(r"sk-(?:proj-)?[^\s'\"]+", "sk-***", text or "")
    return redacted[:300]


def build_agent_input(message: IncomingMessage, customer_context: dict) -> str:
    vip_block = ""
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        vip_block = f"\n\n{build_vip_openai_context(vip, nickname)}\n"

    return f"""
Mensagem recebida via WhatsApp:
- Nome informado: {message.sender_name or 'não informado'}
- Telefone presente: {'sim' if message.sender_phone else 'não'}
- Texto: {message.text}

Contexto mínimo do cadastro:
{customer_context}
{vip_block}
Responda com base na base oficial do site. Não invente saldo, cupom ou resultados.
""".strip()


def generate_agent_reply(message: IncomingMessage, customer_context: dict) -> AgentResult:
    settings = get_settings()

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
    if detect_current_raffle_inquiry(message.text):
        return build_current_raffle_reply(message)

    if detect_third_party_account_inquiry(message.text, message.sender_phone):
        return _third_party_reply()

    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    preferred_reply = build_preferred_name_reply(message, account)
    if preferred_reply:
        return preferred_reply

    if detect_balance_inquiry(message.text):
        return build_balance_reply(message)
    if detect_coupon_code_inquiry(message.text):
        return build_coupon_code_reply(message)
    if detect_simulation_inquiry(message.text):
        return build_simulation_reply(message)
    if detect_raffle_history_inquiry(message.text):
        return build_raffle_history_reply(message)
    if detect_rules_inquiry(message.text):
        return build_rules_reply_result(message)
    if detect_human_support_request(message.text):
        return AgentResult(
            reply_text=HUMAN_SUPPORT_MESSAGE,
            intent="human_support",
            handoff_required=True,
            safety_reason="human_support_requested",
        )

    if message.input_modality == "audio" and not (message.text or "").strip():
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui transcrever agora. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    if not settings.openai_api_key:
        return AgentResult(
            reply_text=(
                "Recebemos sua mensagem. A equipe da New Store vai dar continuidade ao atendimento. "
                f"{HUMAN_SUPPORT_MESSAGE}"
            ),
            intent="fallback_no_openai_key",
            handoff_required=True,
            safety_reason="openai_api_key_missing",
        )

    client = OpenAI(api_key=settings.openai_api_key)
    user_input = build_agent_input(message, customer_context)
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": user_input},
            ],
        )
    except APIStatusError as exc:
        print("[openai.agent] request_failed", {
            "status_code": exc.status_code,
            "error_type": type(exc).__name__,
            "model": settings.openai_model,
            "message": _sanitize_log_message(str(exc)),
        })
        if exc.status_code == 401:
            return AgentResult(
                reply_text=default_safe_handoff(),
                intent="handoff",
                handoff_required=True,
                safety_reason="openai_auth_failed",
            )
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="handoff",
            handoff_required=True,
            safety_reason=f"openai_error_{exc.status_code}",
        )

    reply = _truncate(
        (response.choices[0].message.content if response.choices else None) or default_safe_handoff(),
        settings.max_reply_chars,
    )
    return AgentResult(
        reply_text=reply,
        intent="general_support",
        handoff_required=False,
    )
