"""Read-only order status for the direct agent.

Reuses the legacy ownership checks. The model may only pass an order number or
tax document that the customer actually wrote, or an order number this path
already confirmed. A guessed id is not treated as authorization.
"""
from __future__ import annotations

import asyncio
import re

from app.commerce.commerce_context import CommerceConversationState
from app.commerce.order_service import (
    extract_valid_tax_document,
    find_order_by_customer_document,
    get_order_facts,
)

_READ_TOOLS = frozenset({"search_customer", "list_orders", "get_order_complete"})
_TRACKING_FIELDS = ("estimated_delivery_date", "sending_date", "sending_code", "tracking_url")
_REASONS = {
    "order_customer_mismatch": (
        "owner_not_confirmed",
        "O telefone deste atendimento não confirma o pedido. Peça o CPF do titular e consulte de novo. Não informe status, prazo ou rastreio.",
    ),
    "order_id_required": (
        "identifier_required",
        "Peça o número do pedido ou o CPF do titular. Não invente status.",
    ),
    "order_selection_required": (
        "order_selection_required",
        "Há mais de um pedido para esse documento. Peça o número. Não invente a lista nem o status.",
    ),
    "order_not_found": (
        "order_not_found",
        "Não encontrei esse pedido. Peça para conferir o número ou o CPF.",
    ),
    "order_customer_not_confirmed": (
        "customer_not_confirmed",
        "Não confirmei um único titular para esse documento. Peça o número do pedido.",
    ),
    "invalid_customer_document": (
        "invalid_customer_document",
        "O documento não é válido. Peça o CPF novamente.",
    ),
}


def _customer_text(incoming, history) -> str:
    parts = [incoming.text or ""]
    parts.extend(
        str(turn.get("content") or "")
        for turn in history or []
        if turn.get("role") == "user"
    )
    return "\n".join(parts)


def _token_in_text(token: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.IGNORECASE))


def _stored_reference(continuity) -> str:
    stored = (continuity or {}).get("order_lookup") or {}
    return str(stored.get("order_reference") or "").strip()


def _public_result(agent_result, *, remember: str | None) -> dict:
    data = agent_result.commercial_data or {}
    if data.get("success") and not agent_result.safety_reason:
        tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
        public = {
            "ok": True,
            "order_id": data.get("order_id"),
            "status": data.get("status"),
            "status_group": data.get("status_group"),
            "instruction": (
                "Diga somente estes fatos. estimated_delivery_date é a previsão deste pedido; "
                "se estiver ausente, a consulta não trouxe prazo. Não exponha documento, telefone ou endereço."
            ),
        }
        for key in _TRACKING_FIELDS:
            value = tracking.get(key)
            if isinstance(value, str) and value.strip():
                public[key] = value.strip()
        if remember:
            public["remember_reference"] = remember
        return public
    error, instruction = _REASONS.get(
        agent_result.safety_reason or "",
        ("order_lookup_unavailable", "Não confirme status, prazo ou rastreio sem uma consulta válida."),
    )
    result = {"ok": False, "error": error, "instruction": instruction}
    if remember:
        result["remember_reference"] = remember
    return result


async def _read_execute(name: str, arguments: dict, client):
    if name not in _READ_TOOLS:
        return {"error": "tool_not_allowed"}
    from app.tray.tray_tools import execute_tool
    return await execute_tool(name, arguments, client)


async def lookup_customer_order(*, incoming, history, continuity, adapter, order_reference, document):
    corpus = _customer_text(incoming, history)
    reference = str(order_reference or "").strip() or None
    stored = _stored_reference(continuity)
    if reference and reference.casefold() != stored.casefold() and not _token_in_text(reference, corpus):
        return {
            "ok": False,
            "error": "identifier_not_in_customer_message",
            "instruction": "Esse número não foi escrito pelo cliente. Peça o número do pedido ou o CPF.",
        }
    supplied = re.sub(r"\D+", "", str(document or ""))
    found = extract_valid_tax_document(corpus)
    if supplied:
        if not found or found[1] != supplied:
            return {
                "ok": False,
                "error": "document_not_in_customer_message",
                "instruction": "Use apenas o CPF ou CNPJ que o cliente escreveu. Se não houver, peça o documento.",
            }
        document_kind, document_digits = found
    else:
        document_kind = document_digits = None
    if not reference and not document_digits:
        return {
            "ok": False,
            "error": "identifier_required",
            "instruction": "Peça o número do pedido ou o CPF do titular. Não invente status.",
        }

    state = CommerceConversationState()
    if incoming.sender_phone:
        state.checkout_draft.customer.phone = incoming.sender_phone
    if document_kind == "cpf":
        state.checkout_draft.customer.cpf = document_digits
    # The number is a filter after the document confirms the customer.
    # It is not stored as prior authorization for a phone-only lookup.
    if reference and document_digits:
        state.order_lookup_id = reference

    async def execute(name, arguments):
        return await _read_execute(name, arguments, adapter)

    try:
        async with asyncio.timeout(20):
            if document_digits:
                agent_result = await find_order_by_customer_document(
                    state=state, execute=execute, document_kind=document_kind, document=document_digits,
                )
            else:
                agent_result = await get_order_facts(
                    state=state, execute=execute, order_id=reference, allow_customer_recovery=False,
                )
    except (TimeoutError, asyncio.TimeoutError):
        return {
            "ok": False,
            "error": "order_lookup_unavailable",
            "instruction": "A consulta não respondeu a tempo. Não confirme status, prazo ou rastreio.",
        }
    if document_digits and agent_result.safety_reason == "order_customer_mismatch":
        return {
            "ok": False,
            "error": "document_does_not_match_order",
            "instruction": (
                "O CPF informado não confirma esse pedido. Diga isso e peça para conferir o número ou o documento. "
                "Não peça o mesmo CPF outra vez e não diga que encaminhou para alguém."
            ),
        }
    data = agent_result.commercial_data or {}
    remembered = None
    if data.get("success") and data.get("order_id"):
        remembered = str(data["order_id"])
    elif reference and agent_result.safety_reason == "order_customer_mismatch":
        remembered = reference
    return _public_result(agent_result, remember=remembered)
