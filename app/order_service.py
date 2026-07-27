from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceCartItem,
    CommerceConversationState,
    checkout_missing_fields,
    normalize_variant_identity,
)
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _failure_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "order_failure_status": "status_code",
        "order_failure_code": "tray_error_code",
        "order_failure_name": "tray_error_name",
        "order_failure_type": "tray_error_type",
        "order_failure_field": "tray_error_field",
        "order_failure_fields": "tray_error_fields",
        "order_failure_causes": "tray_error_causes",
        "order_failure_message": "tray_error_message",
    }
    return {
        target: payload[source]
        for target, source in mapping.items()
        if payload.get(source) not in (None, "", [])
    }


def _session_tag(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _money(value: Any) -> str | None:
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, TypeError, ValueError):
        return None


def cart_order_products(
    cart: dict[str, Any],
    factual_items: list[CommerceCartItem] | None = None,
) -> list[dict[str, Any]]:
    factual_prices = {
        (item.product_id, normalize_variant_identity(item.variant_id)): (
            item.unit_price,
            item.original_price,
        )
        for item in factual_items or []
    }
    products: list[dict[str, Any]] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        if product_id is None:
            continue
        try:
            variant_id = normalize_variant_identity(item.get("variant_id"))
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        persisted = factual_prices.get((str(product_id), variant_id), (None, None))
        price = item.get("unit_price")
        if price is None:
            price = item.get("price")
        if price is None:
            price = persisted[0]
        original_price = item.get("original_price")
        if original_price is None:
            original_price = persisted[1]
        normalized_price = _money(price)
        normalized_original_price = _money(original_price)
        if normalized_price is None or Decimal(normalized_price) <= 0 or quantity < 1:
            continue
        if normalized_original_price is not None and Decimal(normalized_original_price) <= 0:
            normalized_original_price = None
        products.append({
            "product_id": str(product_id),
            "variant_id": variant_id,
            "price": normalized_price,
            "original_price": normalized_original_price,
            "quantity": quantity,
        })
    return products

def _preconditions(state: CommerceConversationState) -> list[str]:
    missing: list[str] = []
    if not state.cart_session_id:
        missing.append("cart_session_id")
    if state.checkout_channel_preference != "whatsapp":
        missing.append("checkout_channel_whatsapp")
    if not state.selected_shipping:
        missing.append("selected_shipping")
    elif not any(
        quote.model_dump(mode="json") == state.selected_shipping.model_dump(mode="json")
        for quote in state.shipping_quotes
    ):
        missing.append("selected_shipping_not_in_active_quote")
    elif (
        state.shipping_quote_zipcode
        and state.checkout_draft.address.zip_code
        and state.shipping_quote_zipcode != state.checkout_draft.address.zip_code
    ):
        missing.append("shipping_zipcode_mismatch")
    missing.extend(checkout_missing_fields(state.checkout_draft))
    if not state.selected_payment_option or not state.selected_payment_option.name:
        missing.append("selected_payment_method")
    return missing


async def _current_order_facts(
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> tuple[dict[str, Any] | None, list[str]]:
    missing = _preconditions(state)
    if not state.cart_session_id:
        return None, missing
    try:
        cart = await execute("get_cart_complete", {"session_id": state.cart_session_id})
    except Exception:
        cart = {"error": "commerce_upstream_error"}
    if "error" in cart:
        return None, [*missing, "cart_unavailable"]
    products = cart_order_products(cart, state.cart_items)
    cart_items = cart.get("items") or []
    if not products or len(products) != len(cart_items):
        return None, [*missing, "cart_products"]
    if not cart_items:
        return None, [*missing, "cart_products"]
    if any(product.get("original_price") is None for product in products):
        return None, [*missing, "cart_original_price_missing"]

    draft = state.checkout_draft
    shipping = state.selected_shipping
    payment = state.selected_payment_option
    if missing or shipping is None or payment is None:
        return None, list(dict.fromkeys(missing))
    payload = {
        "session_id": state.cart_session_id,
        "shipping": {
            "shipping_id": shipping.shipping_id,
            "quotation_id": shipping.quotation_id,
            "name": shipping.name,
            "value": shipping.price,
            "min_period": shipping.min_period,
            "max_period": shipping.max_period,
        },
        "payment": {"method_id": payment.id, "name": payment.name},
        "customer": {
            "type": draft.customer.type,
            "name": draft.customer.name,
            "cpf": draft.customer.cpf,
            "email": draft.customer.email,
            "phone": draft.customer.phone,
            **({"rg": draft.customer.rg} if draft.customer.rg else {}),
            **({"gender": draft.customer.gender} if draft.customer.gender else {}),
        },
        "address": {
            "address": draft.address.address,
            "zip_code": draft.address.zip_code,
            "number": draft.address.number,
            "complement": draft.address.complement or "",
            "neighborhood": draft.address.neighborhood,
            "city": draft.address.city,
            "state": draft.address.state,
            "country": draft.address.country,
            "type": draft.address.type,
        },
        "products": products,
    }
    summary_products: list[dict[str, Any]] = []
    subtotal = Decimal("0")
    for raw, product in zip(cart_items, products):
        unit = Decimal(product["price"])
        item_subtotal = unit * product["quantity"]
        subtotal += item_subtotal
        summary_products.append({
            "product_id": product["product_id"],
            "variant_id": product["variant_id"],
            "name": raw.get("name"),
            "variant": raw.get("variant") or raw.get("variant_name"),
            "quantity": product["quantity"],
            "price": product["price"],
            "subtotal": format(item_subtotal.quantize(Decimal("0.01")), "f"),
        })
    cart_subtotal = _money(cart.get("subtotal")) or format(subtotal.quantize(Decimal("0.01")), "f")
    shipping_value = Decimal(shipping.price)
    display_total = Decimal(cart_subtotal) + shipping_value
    summary = {
        "order_ready": True,
        "order_confirmation_pending": True,
        "products": summary_products,
        "cart_subtotal": cart_subtotal,
        "shipping": {
            "name": shipping.name,
            "price": shipping.price,
            "min_period": shipping.min_period,
            "max_period": shipping.max_period,
            "estimated_delivery_date": shipping.estimated_delivery_date,
        },
        "payment": {"id": payment.id, "name": payment.name},
        "customer": {"name": draft.customer.name},
        "delivery": {
            "city": draft.address.city,
            "state": draft.address.state,
            "zipcode": draft.address.zip_code,
        },
        "display_total": format(display_total.quantize(Decimal("0.01")), "f"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"payload": payload, "summary": summary, "version": version}, []


async def prepare_order(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    print("[sales.order.prepare]", {
        "session": _session_tag(state.cart_session_id),
        "has_shipping": bool(state.selected_shipping),
        "has_payment": bool(state.selected_payment_option),
    })
    facts, missing = await _current_order_facts(state, execute)
    if facts is None:
        print("[sales.checkout.missing_fields]", {
            "missing_count": len(missing), "missing_fields": missing,
        })
        return AgentResult(
            reply_text="O pedido ainda n\u00e3o est\u00e1 pronto para revis\u00e3o.",
            intent="commerce",
            safety_reason="order_not_ready",
            commercial_data={
                "order_ready": False,
                "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
                "missing_fields": missing,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "not_ready",
                    "order_review_version": None,
                    "confirmed_order_review_version": None,
                },
                "pending_action": "awaiting_checkout_data",
                "pending_action_product_ids": [],
                "used_tray": bool(state.cart_session_id),
            },
        )
    print("[sales.order.confirmation.pending]", {
        "session": _session_tag(state.cart_session_id),
        "review_version": facts["version"][:10],
    })
    return AgentResult(
        reply_text="Resumo factual do pedido preparado.",
        intent="commerce",
        commercial_data=facts["summary"],
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_confirmation_status": "pending",
                "order_review_version": facts["version"],
                "confirmed_order_review_version": None,
            },
            "purchase_stage": "order_review",
            "pending_action": "awaiting_order_confirmation",
            "pending_action_product_ids": [],
            "used_tray": True,
        },
    )


def confirm_prepared_order(state: CommerceConversationState) -> AgentResult:
    allowed = bool(
        state.order_confirmation_status == "pending"
        and state.order_review_version
    )
    if not allowed:
        return AgentResult(
            reply_text="N\u00e3o h\u00e1 resumo atual aguardando confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_missing",
            commercial_data={"success": False, "stage": "order_confirmation"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    print("[sales.order.confirmation.accepted]", {
        "session": _session_tag(state.cart_session_id),
        "review_version": state.order_review_version[:10],
    })
    return AgentResult(
        reply_text="Confirma\u00e7\u00e3o expl\u00edcita vinculada ao resumo atual.",
        intent="commerce",
        commercial_data={"success": True, "stage": "order_confirmation"},
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_confirmation_status": "confirmed",
                "order_review_version": state.order_review_version,
                "confirmed_order_review_version": state.order_review_version,
            },
            "clear_pending_action": True,
            "used_tray": False,
        },
    )


def _existing_order(orders: dict[str, Any]) -> dict[str, Any] | None:
    values = orders.get("orders") if isinstance(orders.get("orders"), list) else []
    return next(
        (order for order in values if isinstance(order, dict) and (order.get("order_id") or order.get("id"))),
        None,
    )


async def create_order(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if state.order_id:
        return AgentResult(
            reply_text="Pedido existente recuperado do estado.",
            intent="commerce",
            commercial_data={
                "success": True, "existing": True, "order_id": state.order_id,
                "status": state.order_status,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    if (
        state.order_confirmation_status != "confirmed"
        or not state.order_review_version
        or state.confirmed_order_review_version != state.order_review_version
    ):
        return AgentResult(
            reply_text="A cria\u00e7\u00e3o do pedido est\u00e1 bloqueada sem confirma\u00e7\u00e3o do resumo atual.",
            intent="commerce",
            safety_reason="order_confirmation_required",
            commercial_data={"success": False, "stage": "order_creation"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    facts, missing = await _current_order_facts(state, execute)
    if facts is None:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram ap\u00f3s a confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data={
                "success": False, "stage": "order_creation",
                "missing_fields": missing,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "not_ready",
                    "order_review_version": None,
                    "confirmed_order_review_version": None,
                },
                "pending_action": "awaiting_checkout_data",
                "pending_action_product_ids": [],
                "used_tray": True,
            },
        )
    if facts["version"] != state.confirmed_order_review_version:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram ap\u00f3s a confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data=facts["summary"],
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "pending",
                    "order_review_version": facts["version"],
                    "confirmed_order_review_version": None,
                },
                "purchase_stage": "order_review",
                "pending_action": "awaiting_order_confirmation",
                "pending_action_product_ids": [],
                "used_tray": True,
            },
        )
    reconciled = None
    if state.order_creation_ambiguous and state.cart_session_id:
        try:
            preflight = await execute(
                "list_orders", {"session_id": state.cart_session_id}
            )
        except Exception:
            preflight = {"error": "commerce_upstream_error"}
        if "error" in preflight:
            return AgentResult(
                reply_text="A cria\u00e7\u00e3o anterior ainda precisa ser reconciliada.",
                intent="commerce",
                safety_reason="order_creation_technical_failure",
                commercial_data={
                    "success": False, "stage": "order_creation", "recoverable": True,
                },
                response_metadata={
                    "domain": "commerce",
                    "order_state": {"order_creation_ambiguous": True},
                    "used_tray": True,
                },
            )
        reconciled = _existing_order(preflight)
        print("[sales.order.reconcile]", {
            "session": _session_tag(state.cart_session_id),
            "found": reconciled is not None,
            "preflight": True,
        })
    if reconciled is None:
        print("[sales.order.create.request]", {
            "session": _session_tag(state.cart_session_id),
            "product_count": len(facts["payload"]["products"]),
            "has_customer": True,
            "address_complete": True,
        })
        try:
            result = await execute("create_order", facts["payload"])
        except Exception as exc:
            result = {
                "error": "commerce_upstream_error",
                "status_code": None,
                "error_type": type(exc).__name__,
            }
    else:
        result = reconciled
    ambiguous = "error" in result and result.get("status_code") in {None, 502, 503, 504}
    if ambiguous and state.cart_session_id:
        try:
            lookup = await execute("list_orders", {"session_id": state.cart_session_id})
        except Exception:
            lookup = {"error": "commerce_upstream_error"}
        reconciled = None if "error" in lookup else _existing_order(lookup)
        print("[sales.order.reconcile]", {
            "session": _session_tag(state.cart_session_id),
            "found": reconciled is not None,
        })
    effective = reconciled or result
    order_id = effective.get("order_id") or effective.get("id")
    if "error" in effective or order_id is None:
        print("[sales.order.create.result]", {
            "session": _session_tag(state.cart_session_id), "success": False,
        })
        return AgentResult(
            reply_text="A cria\u00e7\u00e3o do pedido n\u00e3o foi confirmada pela integra\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_creation_technical_failure",
            commercial_data={
                "success": False,
                "stage": "order_creation",
                "recoverable": ambiguous,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {"order_creation_ambiguous": ambiguous},
                "used_tray": True,
                **_failure_metadata(effective),
            },
        )
    print("[sales.order.create.result]", {
        "session": _session_tag(state.cart_session_id),
        "success": True,
        "reconciled": reconciled is not None,
    })
    status = effective.get("status")
    return AgentResult(
        reply_text="Pedido criado e identificado pela integra\u00e7\u00e3o.",
        intent="commerce",
        commercial_data={
            "success": True,
            "order_id": str(order_id),
            "status": status,
            "status_group": effective.get("status_group"),
        },
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_confirmation_status": "not_ready",
                "order_review_version": None,
                "confirmed_order_review_version": None,
                "order_id": str(order_id),
                "order_status": status,
                "order_status_group": effective.get("status_group"),
                "order_session_id": state.cart_session_id,
                "order_created_at": effective.get("created_at") or effective.get("order_created_at"),
                "order_creation_ambiguous": False,
            },
            "purchase_stage": "order_created",
            "clear_pending_action": True,
            "used_tray": True,
        },
    )


async def get_order_facts(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    order_id: str | None = None,
) -> AgentResult:
    target = str(order_id or state.order_id or "").strip()
    if not target and (state.order_session_id or state.cart_session_id):
        session_id = state.order_session_id or state.cart_session_id
        try:
            lookup = await execute("list_orders", {"session_id": session_id})
        except Exception:
            lookup = {"error": "commerce_upstream_error"}
        existing = None if "error" in lookup else _existing_order(lookup)
        if existing is not None:
            target = str(existing.get("order_id") or existing.get("id") or "").strip()
        print("[sales.order.reconcile]", {
            "session": _session_tag(session_id),
            "found": bool(target),
            "status_lookup": True,
        })
    if not target:
        return AgentResult(
            reply_text="N\u00e3o h\u00e1 pedido identificado para consulta.",
            intent="commerce",
            safety_reason="order_id_required",
            commercial_data={"success": False, "stage": "order_status"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    try:
        result = await execute("get_order_complete", {"order_id": target})
    except Exception:
        result = {"error": "commerce_upstream_error"}
    if "error" in result:
        return AgentResult(
            reply_text="A consulta atual do pedido n\u00e3o p\u00f4de ser conclu\u00edda.",
            intent="commerce",
            safety_reason="order_status_technical_failure",
            commercial_data={"success": False, "stage": "order_status"},
            response_metadata={
                "domain": "commerce",
                "used_tray": True,
                **_failure_metadata(result),
            },
        )
    shipment = result.get("shipment")
    shipment = shipment if isinstance(shipment, dict) else {}
    tracking = {
        key: result.get(key)
        for key in (
            "sending_code", "tracking_url", "sending_date",
            "estimated_delivery_date", "shipment",
        ) if result.get(key) is not None
    }
    for key in (
        "sending_code", "tracking_url", "sending_date",
        "estimated_delivery_date",
    ):
        if key not in tracking and shipment.get(key) is not None:
            tracking[key] = shipment[key]
    if shipment:
        tracking["shipment"] = shipment
    facts = {
        "success": True,
        "order_id": str(result.get("order_id") or result.get("id") or target),
        "status": result.get("status"),
        "status_group": result.get("status_group"),
        "tracking": tracking,
    }
    print("[sales.order.status]", {
        "order_id_present": True,
        "status": facts["status"],
        "status_group": facts["status_group"],
        "tracking_present": bool(tracking),
    })
    return AgentResult(
        reply_text="Status atual do pedido consultado.",
        intent="commerce",
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_id": facts["order_id"],
                "order_status": facts["status"],
                "order_status_group": facts["status_group"],
            },
            "purchase_stage": (
                "payment_confirmed"
                if state.order_payment_status == "confirmed"
                else "awaiting_payment"
                if state.order_payment_status == "pending"
                else "order_created"
            ),
            "used_tray": True,
        },
    )
