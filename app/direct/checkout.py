"""Read-only proposals plus a deterministic, separately confirmed site checkout.

Only handle_checkout_command may mutate. It runs outside the model tool loop.
An uncertain execution consumes its claim permanently; there is no blind retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
import unicodedata
from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.catalog.retrieval.price import money_decimal, resolve_commercial_price
from app.models import AgentResult


class CheckoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_id: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    variant_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,17}$")
    quantity: int = Field(default=1, ge=1, le=10)


class Proposal(CheckoutItem):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace: str
    scope: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: int
    name: str = Field(min_length=1, max_length=300)
    variant_label: str = Field(max_length=300)
    unit_price: str = Field(pattern=r"^[0-9]+\.[0-9]{2}$")


def checkout_scope(workspace, incoming):
    identity = incoming.sender_key or incoming.sender_phone or incoming.visitor_id
    if not workspace or not identity or not incoming.conversation_id:
        raise ValueError("checkout_identity_required")
    return hashlib.sha256(json.dumps([str(workspace), incoming.provider, incoming.channel,
        incoming.conversation_id, identity], ensure_ascii=False).encode()).hexdigest()


def _true(value):
    return value is True or str(value).lower() in {"1", "true"}


async def current_item(adapter, item: CheckoutItem):
    # Bypass the catalog cache: both the review and confirmation need live facts.
    payload = await adapter._request("GET", f"/internal/products/{item.product_id}")
    product = payload.get("product", {})
    if str(product.get("id")) != item.product_id:
        raise ValueError("product_identity_mismatch")
    if not _true(product.get("available")) or product.get("available_for_purchase") in {False, "0", 0}:
        raise ValueError("product_unavailable")
    effective = product
    label = ""
    if item.variant_id:
        payload = await adapter.get_product_variant(item.variant_id)
        effective = payload.get("variant", {})
        if (str(effective.get("id")) != item.variant_id
                or str(effective.get("product_id")) != item.product_id):
            raise ValueError("variant_identity_mismatch")
        label = ", ".join(str(v.get("value", "")) for v in effective.get("sku", []) if isinstance(v, dict))
        label = label or str(effective.get("reference") or item.variant_id)
    elif _true(product.get("has_variation")) or product.get("variants"):
        raise ValueError("variant_required")
    stock = money_decimal(effective.get("stock"))
    if stock is None or stock < item.quantity:
        raise ValueError("stock_unavailable")
    price = resolve_commercial_price(effective, require_positive=True).amount
    if price is None or price <= 0 or price != price.quantize(Decimal("0.01")):
        raise ValueError("current_price_unavailable")
    name = str(product.get("name") or "").strip()
    if not name:
        raise ValueError("product_name_unavailable")
    return {"name": name[:300], "variant_label": label[:300], "unit_price": f"{price:.2f}"}


async def prepare_checkout(*, adapter, item, incoming, workspace, known_ids):
    if item.product_id not in known_ids:
        return {"ok": False, "error": "known_admin_product_required"}, None
    scope = checkout_scope(workspace, incoming)
    async with asyncio.timeout(20):
        facts = await current_item(adapter, item)
    proposal = Proposal(**item.model_dump(), **facts, id=secrets.token_hex(16),
        workspace=str(workspace), scope=scope, expires_at=int(time.time()) + 600).model_dump()
    return {"ok": True, "requires_confirmation": True, "mutated": False}, proposal


def _result(text, *, proposal=None, status, reason=None, cart_url=None):
    metadata = {"engine": "direct", "response_source": "direct_checkout",
        # No remote conversation is reused after a deterministic reply.
        "direct_agent": {"calls": 0, "tools": [], "checkout_status": status},
        "direct_checkout": {"status": status, "proposal": proposal}}
    if cart_url:
        metadata["direct_checkout"]["cart_url"] = cart_url
    return AgentResult(reply_text=text, intent="commerce", safety_reason=reason,
        response_metadata=metadata)


def review_result(proposal):
    code = proposal["id"][:8].upper()
    total = Decimal(proposal["unit_price"]) * proposal["quantity"]
    variant = f" ({proposal['variant_label']})" if proposal["variant_label"] else ""
    text = (f"Confira: {proposal['quantity']} × {proposal['name']}{variant}, "
            f"R$ {proposal['unit_price'].replace('.', ',')} por unidade. "
            f"Subtotal: R$ {total:.2f}.\n\n"
            "Frete e condições finais de pagamento serão mostrados no site. "
            "O carrinho não reserva estoque nem confirma pagamento.\n\n"
            f"Para criar o carrinho, responda CONFIRMAR {code}. "
            f"Para desistir desta proposta, CANCELAR {code}. Válido por 10 minutos.")
    return _result(text, proposal=proposal, status="awaiting_confirmation")


def _normalized(text):
    value = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                    if not unicodedata.combining(c))
    return " ".join(value.strip().split())


def _verified_cart(raw, proposal):
    cart = raw.get("cart", {})
    items = cart.get("items", [])
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("cart_contents_mismatch")
    item = items[0]
    variant = item.get("variant_id")
    variant = str(variant) if variant not in (None, "", 0, "0") else None
    if (str(item.get("product_id")) != proposal["product_id"]
            or variant != proposal["variant_id"]
            or item.get("quantity") != proposal["quantity"]
            or money_decimal(item.get("price")) != Decimal(proposal["unit_price"])):
        raise ValueError("cart_contents_mismatch")
    session = cart.get("session_id")
    if session is not None and session != proposal["id"]:
        raise ValueError("cart_session_mismatch")
    return cart


def _cart_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("cart_url_unavailable")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("cart_url_unavailable")
    return value


async def execute_checkout(proposal, adapter, journal):
    """At most one POST per proposal, even after crashes or failed delivery."""
    owned, receipt = await asyncio.to_thread(journal.claim, proposal)
    if not owned:
        if receipt["status"] == "completed":
            url = _cart_url(receipt["result"].get("cart_url"))
            return _result(f"Seu carrinho já foi preparado. Confira o frete e finalize no site:\n{url}",
                           proposal=proposal, status="completed", cart_url=url)
        return _result("Esta confirmação já foi processada, mas não tenho um carrinho validado para enviar. "
                       "Peça atendimento humano para verificar antes de tentar novamente.",
                       proposal=proposal, status=receipt["status"], reason="checkout_reconciliation_required")
    try:
        async with asyncio.timeout(20):
            facts = await current_item(adapter, CheckoutItem.model_validate({k: proposal[k]
                for k in ("product_id", "variant_id", "quantity")}))
        if any(facts[k] != proposal[k] for k in ("unit_price", "name", "variant_label")):
            raise ValueError("checkout_facts_changed")
    except Exception:
        await asyncio.to_thread(journal.finish, proposal, "rejected", {})
        return _result("Não consegui revalidar as condições desta proposta. "
                       "Peça uma nova revisão do produto e do preço antes de confirmar.",
                       status="rejected", reason="checkout_revalidation_required")
    # The durable claim is already committed. Never clear it on timeout/cancellation.
    try:
        async with asyncio.timeout(30):
            created = await adapter.create_cart(product_id=proposal["product_id"],
                variant_id=proposal["variant_id"], quantity=proposal["quantity"],
                price=proposal["unit_price"], session_id=proposal["id"])
            cart = created.get("cart", {})
            if created.get("success") is not True or cart.get("session_id") != proposal["id"]:
                raise ValueError("cart_creation_unverified")
            url = _cart_url(cart.get("cart_url"))
            _verified_cart(await adapter.get_cart_complete(proposal["id"]), proposal)
        await asyncio.to_thread(journal.finish, proposal, "completed", {"cart_url": url})
        return _result(f"Carrinho preparado. Confira o frete e finalize o pagamento no site:\n{url}\n\n"
                       "O pedido e o pagamento ainda não estão confirmados.",
                       proposal=proposal, status="completed", cart_url=url)
    except Exception:
        # If even this write fails, the original 'started' claim still prevents replay.
        await asyncio.to_thread(journal.finish, proposal, "unknown", {})
        return _result("Não consegui confirmar o resultado da criação do carrinho. "
                       "Peça atendimento humano para verificar; não vou repetir a operação automaticamente.",
                       proposal=proposal, status="unknown", reason="checkout_reconciliation_required")


async def handle_checkout_command(incoming, previous, workspace, *, enabled, preview=False,
                                  adapter=None, journal=None, now=None):
    state = previous.get("direct_checkout") or {}
    text = _normalized(incoming.text or "")
    command = re.fullmatch(r"(confirmar|cancelar) ([a-f0-9]{8})[.!]?", text)
    if not command:
        return None
    if not enabled:
        return _result("A criação de carrinho por aqui está indisponível. Use o link oficial do produto.",
                       status="disabled", reason="checkout_disabled")
    try:
        proposal = Proposal.model_validate(state.get("proposal")).model_dump()
        if proposal["scope"] != checkout_scope(workspace, incoming) or proposal["workspace"] != str(workspace):
            raise ValueError("checkout_scope_mismatch")
        if command[2] != proposal["id"][:8]:
            raise ValueError("checkout_code_mismatch")
    except (ValidationError, ValueError, TypeError):
        return _result("Não encontrei uma proposta válida com esse código nesta conversa. "
                       "Peça uma nova revisão do produto.", status="invalid", reason="checkout_invalid_confirmation")
    if (incoming.audio_url or incoming.image_url or incoming.instagram_story
            or incoming.input_modality != "text" or incoming.attachment_type):
        return _result("Envie a confirmação em uma mensagem de texto separada, sem anexos.",
                       proposal=proposal, status=state.get("status", "awaiting_confirmation"))
    if command[1] == "cancelar":
        if state.get("status") != "awaiting_confirmation":
            return _result("Esta operação já saiu da etapa de proposta. Para cancelar um carrinho ou pedido, "
                           "peça atendimento humano.", proposal=proposal, status=state.get("status", "unknown"))
        return _result("Proposta descartada. Nenhum pedido foi cancelado.", status="cancelled")
    if proposal["expires_at"] <= (time.time() if now is None else now):
        return _result("A proposta expirou. Peça uma nova revisão para conferir preço e disponibilidade.",
                       status="expired", reason="checkout_expired")
    if preview:
        return _result("Simulação: a confirmação foi reconhecida. Nenhum carrinho, pedido ou pagamento foi criado.",
                       proposal=proposal, status="preview")
    from app.evaluation.context import current_evaluation
    if current_evaluation() is not None or incoming.provider == "test":
        return _result("Execução comercial indisponível neste ambiente de teste.", status="preview")
    from app.tray.tray_adapter_client import TrayAdapterClient
    from app.direct.checkout_journal import CheckoutJournal
    try:
        return await execute_checkout(proposal, adapter or TrayAdapterClient(), journal or CheckoutJournal())
    except Exception:
        return _result("Não consegui verificar o registro desta operação. "
                       "Peça atendimento humano antes de tentar novamente.",
                       proposal=proposal, status="unknown", reason="checkout_journal_unavailable")


def carry_checkout_context(result, previous):
    """Persist only through the existing delivered-response audit, never model memory."""
    if "direct_checkout" not in result.response_metadata and previous.get("direct_checkout"):
        result.response_metadata["direct_checkout"] = previous["direct_checkout"]
    if result.response_metadata.get("response_source") == "direct_checkout":
        # Retain known products/continuity without retaining an incompatible remote tail.
        old = previous.get("direct_agent") or {}
        for key in ("products", "continuity", "catalog_snapshot", "last_catalog_search"):
            if key in old:
                result.response_metadata["direct_agent"].setdefault(key, old[key])
    return result
