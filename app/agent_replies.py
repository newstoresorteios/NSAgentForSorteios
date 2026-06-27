from __future__ import annotations

import re

from .guardrails import default_safe_handoff
from .models import AgentResult, IncomingMessage
from .repository import (
    find_coupon_balance_by_phone,
    find_current_raffle,
    find_user_coupon_code,
    find_user_raffle_participation,
    format_cents_to_brl,
)
from .site_knowledge import (
    REGISTER_PHONE_MESSAGE,
    SITE_URL,
    STORE_URL,
    THIRD_PARTY_REFUSAL,
    build_rules_reply,
    build_simulation_reply as build_simulation_text,
)


def _greeting(name: str | None) -> str:
    cleaned = (name or "").strip()
    return f"Olá, {cleaned}!" if cleaned else "Olá!"


def _account_missing_reply(intent: str) -> AgentResult:
    return AgentResult(
        reply_text=REGISTER_PHONE_MESSAGE,
        intent=intent,
        handoff_required=False,
        safety_reason="account_phone_not_registered",
    )


def _third_party_reply() -> AgentResult:
    return AgentResult(
        reply_text=THIRD_PARTY_REFUSAL,
        intent="security_refusal",
        handoff_required=False,
        safety_reason="third_party_account_inquiry",
    )


def build_balance_reply(message: IncomingMessage) -> AgentResult:
    account = find_coupon_balance_by_phone(message.sender_phone, message.text)

    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()

    if account.get("error") == "phone_missing":
        return AgentResult(
            reply_text=REGISTER_PHONE_MESSAGE,
            intent="balance_inquiry",
            handoff_required=False,
            safety_reason="phone_missing",
        )

    if account.get("error") == "database_not_configured":
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="balance_inquiry",
            handoff_required=True,
            safety_reason="database_not_configured",
        )

    if account.get("lookup_error"):
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="balance_inquiry",
            handoff_required=True,
            safety_reason="balance_lookup_failed",
        )

    if account.get("error") == "phone_not_registered":
        return _account_missing_reply("balance_inquiry")

    if not account.get("found"):
        return AgentResult(
            reply_text=REGISTER_PHONE_MESSAGE,
            intent="balance_inquiry",
            handoff_required=False,
        )

    name = account.get("name") or message.sender_name
    return AgentResult(
        reply_text=f"{_greeting(name)} Seu saldo disponível é {account['balance_brl']}.",
        intent="balance_inquiry",
        handoff_required=False,
    )


def build_coupon_code_reply(message: IncomingMessage) -> AgentResult:
    account = find_user_coupon_code(message.sender_phone, message.text)

    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()
    if account.get("error") == "phone_missing":
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="coupon_code", handoff_required=False)
    if account.get("error") == "phone_not_registered":
        return _account_missing_reply("coupon_code")
    if not account.get("found"):
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="coupon_code", handoff_required=False)
    if account.get("lookup_error"):
        return AgentResult(reply_text=default_safe_handoff(), intent="coupon_code", handoff_required=True)

    code = account.get("coupon_code") or "indisponível"
    balance = account.get("balance_brl") or format_cents_to_brl(0)
    name = account.get("name") or message.sender_name
    return AgentResult(
        reply_text=(
            f"{_greeting(name)} Seu Cartão Presente: código *{code}* | saldo {balance}. "
            f"Use em {STORE_URL} no checkout. Código pessoal e intransferível."
        ),
        intent="coupon_code",
        handoff_required=False,
    )


def build_simulation_reply(message: IncomingMessage) -> AgentResult:
    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()

    amount_match = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?|\d+)", message.text or "")
    if account.get("found"):
        credit_cents = int(account.get("coupon_value_cents") or 0)
    elif amount_match:
        raw = amount_match.group(1).replace(".", "").replace(",", ".")
        try:
            credit_cents = int(float(raw) * 100)
        except ValueError:
            credit_cents = 0
    else:
        return AgentResult(
            reply_text=(
                f"Para simular o uso do Cartão Presente, informe um valor (ex.: R$ 800) ou cadastre seu telefone em {SITE_URL} "
                "para eu usar seu saldo real."
            ),
            intent="simulation",
            handoff_required=False,
        )

    return AgentResult(
        reply_text=build_simulation_text(credit_cents),
        intent="simulation",
        handoff_required=False,
    )


def build_current_raffle_reply() -> AgentResult:
    raffle = find_current_raffle()
    if raffle.get("lookup_error"):
        return AgentResult(reply_text=default_safe_handoff(), intent="current_raffle", handoff_required=True)

    if not raffle.get("found"):
        return AgentResult(
            reply_text=(
                f"Consulte o sorteio atual em {SITE_URL}. "
                "Quando a rodada estiver aberta, você escolhe o número na página e acompanha pelo grupo oficial."
            ),
            intent="current_raffle",
            handoff_required=False,
        )

    lines = [
        f"Sorteio atual: {raffle.get('title') or 'Rodada aberta'}.",
        f"Prêmio: {raffle.get('prize_name') or 'consulte o site'}.",
        f"Status: {raffle.get('status') or 'aberto'}.",
    ]
    if raffle.get("quota_price_brl"):
        lines.append(f"Valor da cota: {raffle['quota_price_brl']}.")
    lines.append(f"Participe em {SITE_URL}.")
    return AgentResult(
        reply_text=" ".join(lines),
        intent="current_raffle",
        handoff_required=False,
    )


def build_raffle_history_reply(message: IncomingMessage) -> AgentResult:
    account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    if account.get("error") == "third_party_inquiry":
        return _third_party_reply()
    if not account.get("found"):
        return AgentResult(reply_text=REGISTER_PHONE_MESSAGE, intent="raffle_history", handoff_required=False)

    history = find_user_raffle_participation(account["user_id"])
    if history.get("lookup_error"):
        return AgentResult(reply_text=default_safe_handoff(), intent="raffle_history", handoff_required=True)

    if not history.get("found"):
        return AgentResult(
            reply_text=(
                f"Ainda não encontramos participações vinculadas ao seu cadastro. "
                f"Confira sorteios passados e resultados em {SITE_URL}."
            ),
            intent="raffle_history",
            handoff_required=False,
        )

    chunks: list[str] = []
    for item in history.get("items", [])[:5]:
        parts = [item.get("title") or "Sorteio"]
        if item.get("numbers"):
            parts.append(f"seus números: {item['numbers']}")
        if item.get("winning_number"):
            parts.append(f"número sorteado: {item['winning_number']}")
        if item.get("winner_name"):
            parts.append(f"vencedor: {item['winner_name']}")
        chunks.append(" | ".join(parts))

    return AgentResult(
        reply_text="Suas participações recentes: " + " // ".join(chunks),
        intent="raffle_history",
        handoff_required=False,
    )


def build_rules_reply_result() -> AgentResult:
    return AgentResult(
        reply_text=build_rules_reply(),
        intent="rules_faq",
        handoff_required=False,
    )
