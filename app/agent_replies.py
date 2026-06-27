from __future__ import annotations

import re
from typing import Any

from .guardrails import default_safe_handoff
from .models import AgentResult, IncomingMessage
from .repository import (
    find_coupon_balance_by_phone,
    find_current_raffle,
    find_last_payment_participation,
    find_user_coupon_code,
    find_user_raffle_participation,
    format_cents_to_brl,
)
from .user_preferences import (
    detect_preferred_name_update,
    get_user_preferences,
    mark_preferred_name_prompted,
    resolve_display_name,
    save_preferred_name,
)
from .site_knowledge import (
    REGISTER_PHONE_MESSAGE,
    SITE_URL,
    STORE_URL,
    THIRD_PARTY_REFUSAL,
    build_rules_reply,
    build_simulation_reply as build_simulation_text,
)


def _greeting(display_name: str | None) -> str:
    cleaned = (display_name or "").strip()
    return f"Olá, {cleaned}!" if cleaned else "Olá!"


def _format_last_participation(user_id: int) -> str | None:
    last_payment = find_last_payment_participation(user_id)
    if not last_payment.get("found"):
        return None

    parts: list[str] = ["Última participação:"]
    if last_payment.get("raffle_title"):
        parts.append(str(last_payment["raffle_title"]))
    if last_payment.get("participated_at"):
        parts.append(f"em {last_payment['participated_at'][:10]}")
    if last_payment.get("amount_brl"):
        parts.append(f"({last_payment['amount_brl']})")
    return " ".join(parts)


def _personalized_suffix(user_id: int, preferences: dict[str, Any]) -> str:
    if preferences.get("preferred_name"):
        return ""
    if not preferences.get("ask_preferred_name", True):
        return ""
    mark_preferred_name_prompted(user_id)
    return " Prefere ser chamado por outro nome? É só me dizer."


def build_preferred_name_reply(message: IncomingMessage, account: dict[str, Any]) -> AgentResult | None:
    preferred_name = detect_preferred_name_update(message.text)
    if not preferred_name or not account.get("found"):
        return None

    save_preferred_name(int(account["user_id"]), preferred_name)
    return AgentResult(
        reply_text=f"Perfeito! A partir de agora vou te chamar de {preferred_name}.",
        intent="preferred_name_update",
        handoff_required=False,
    )


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

    user_id = int(account["user_id"])
    preferences = get_user_preferences(user_id)
    display_name = resolve_display_name(account.get("name"), preferences)
    parts = [f"{_greeting(display_name)} Seu saldo disponível é {account['balance_brl']}."]

    last_participation = _format_last_participation(user_id)
    if last_participation:
        parts.append(last_participation)

    parts.append(_personalized_suffix(user_id, preferences).strip())

    return AgentResult(
        reply_text=" ".join(part for part in parts if part),
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
    user_id = int(account["user_id"])
    preferences = get_user_preferences(user_id)
    display_name = resolve_display_name(account.get("name"), preferences)
    suffix = _personalized_suffix(user_id, preferences)
    return AgentResult(
        reply_text=(
            f"{_greeting(display_name)} Seu Cartão Presente: código *{code}* | saldo {balance}. "
            f"Use em {STORE_URL} no checkout. Código pessoal e intransferível.{suffix}"
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
    last_payment = find_last_payment_participation(int(account["user_id"]))
    if history.get("lookup_error"):
        return AgentResult(reply_text=default_safe_handoff(), intent="raffle_history", handoff_required=True)

    if not history.get("found"):
        if last_payment.get("found"):
            title = last_payment.get("raffle_title") or "sorteio"
            return AgentResult(
                reply_text=(
                    f"Sua última participação registrada foi em {last_payment['participated_at'][:10]} "
                    f"({title})."
                ),
                intent="raffle_history",
                handoff_required=False,
            )
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
