from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .checkout_service import checkout_capabilities
from .commerce_context import CommerceConversationState, checkout_missing_fields
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _payment_failure_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "payment_failure_status": "status_code",
        "payment_failure_code": "tray_error_code",
        "payment_failure_name": "tray_error_name",
        "payment_failure_type": "tray_error_type",
        "payment_failure_field": "tray_error_field",
        "payment_failure_fields": "tray_error_fields",
        "payment_failure_causes": "tray_error_causes",
        "payment_failure_message": "tray_error_message",
    }
    return {
        target: payload[source]
        for target, source in mapping.items()
        if payload.get(source) not in (None, "", [])
    }


def _no_cart() -> AgentResult:
    return AgentResult(
        reply_text="Ainda não há um carrinho ativo para consultar.",
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_validation_error",
    )


async def inspect_current_cart(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if not state.cart_session_id:
        return _no_cart()
    cart = await execute(
        "get_cart_complete",
        {"session_id": state.cart_session_id},
    )
    if "error" in cart:
        return AgentResult(
            reply_text="Não consegui consultar o carrinho neste momento.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_technical_failure",
            response_metadata={"used_tray": True},
        )
    print("[sales.cart.verify]", {
        "item_count": len(cart.get("items") or []),
        "has_total": any(
            cart.get(key) is not None
            for key in ("total", "current_total", "subtotal")
        ),
    })
    return AgentResult(
        reply_text="Consultei o estado atual do seu carrinho.",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "cart": cart,
            "checkout": checkout_capabilities(state),
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "cart_created",
            "used_tray": True,
        },
    )


async def inspect_payment_options(
    *,
    state: CommerceConversationState,
    installment_count: int | None,
    payment_method_preference: str | None = None,
    execute: ToolExecutor,
    payment_option_id: str | None = None,
) -> AgentResult:
    if not state.cart_session_id:
        return _no_cart()
    result = await execute(
        "get_payment_options",
        {"cart_session_id": state.cart_session_id},
    )
    if "error" in result:
        print("[sales.purchase.payment]", {
            "has_cart_session": True,
            "requested_method": payment_method_preference,
            "options_loaded": False,
            "method_available": None,
        })
        return AgentResult(
            reply_text="Não consegui consultar as formas de pagamento neste momento.",
            intent="commerce",
            handoff_required=False,
            safety_reason="payment_options_technical_failure",
            response_metadata={"used_tray": True},
        )
    options = result.get("payment_options")
    options = options if isinstance(options, dict) else {}
    installments = options.get("installments")
    installments = installments if isinstance(installments, list) else []
    selected = None
    if installment_count is not None:
        selected = next(
            (
                item
                for item in installments
                if isinstance(item, dict)
                and item.get("count") == installment_count
            ),
            None,
        )
    method_available: bool | None = None
    selected_option: dict[str, Any] | None = None
    available_options = options.get("options")
    available_options = available_options if isinstance(available_options, list) else []
    if payment_option_id is not None:
        selected_option = next(
            (
                option for option in available_options
                if isinstance(option, dict)
                and str(option.get("id")) == str(payment_option_id)
            ),
            None,
        )
        method_available = selected_option is not None
    elif payment_method_preference == "pix":
        selected_option = (
            options.get("pix")
            if isinstance(options.get("pix"), dict)
            else None
        )
        method_available = selected_option is not None
    elif payment_method_preference == "card":
        selected_option = (
            options.get("card")
            if isinstance(options.get("card"), dict)
            else None
        )
        method_available = (
            selected_option is not None
            or bool(installments)
        )
    elif payment_method_preference == "boleto":
        selected_option = (
            options.get("boleto")
            if isinstance(options.get("boleto"), dict)
            else None
        )
        method_available = selected_option is not None
    print("[sales.payment.options]", {
        "has_session_id": True,
        "option_count": (
            len(options.get("options"))
            if isinstance(options.get("options"), list)
            else len(installments) + int("pix" in options) + int("boleto" in options)
        ),
    })
    print("[sales.purchase.payment]", {
        "has_cart_session": True,
        "requested_method": payment_method_preference,
        "options_loaded": True,
        "method_available": method_available,
    })
    facts: dict[str, Any] = {
        "payment_options": options,
        "requested_method": payment_method_preference,
        "requested_payment_option_id": payment_option_id,
        "requested_method_available": method_available,
        "requested_installment_count": installment_count,
        "requested_installment": selected,
        "cart_url": state.cart_url,
        "checkout": checkout_capabilities(state),
    }
    if payment_method_preference is not None and method_available is False:
        reply = (
            "A forma de pagamento escolhida não aparece entre as opções "
            "reais deste carrinho."
        )
    elif installment_count is not None and selected is None:
        reply = (
            f"A Tray não informou uma opção de {installment_count} parcelas "
            "para este carrinho."
        )
    else:
        reply = "Consultei as formas de pagamento reais deste carrinho."
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason=(
            "payment_method_unavailable"
            if (payment_method_preference is not None or payment_option_id is not None)
            and method_available is False
            else None
        ),
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
            **(
                {
                    "selected_payment_method": payment_method_preference,
                    "selected_payment_option": {
                        "id": (
                            str(selected_option["id"])
                            if selected_option.get("id") is not None else None
                        ),
                        "name": str(selected_option["name"]),
                        "method": payment_method_preference,
                    },
                    **(
                        {"selected_payment_option_id": str(selected_option["id"])}
                        if selected_option.get("id") is not None else {}
                    ),
                }
                if method_available is True
                and selected_option is not None
                and selected_option.get("name")
                else {
                    "selected_payment_method": payment_method_preference,
                }
                if method_available is True
                and payment_method_preference is not None
                else {}
            ),
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if state.checkout_channel_preference is None
                else {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if (
                    state.checkout_channel_preference == "whatsapp"
                    and checkout_missing_fields(state.checkout_draft)
                )
                else {"clear_pending_action": True}
            ),
            "used_tray": True,
        },
    )


async def inspect_order_payment(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    order_id: str | None = None,
) -> AgentResult:
    target = str(order_id or state.order_id or "").strip()
    if not target:
        return AgentResult(
            reply_text="Nao ha pedido identificado para consultar o pagamento.",
            intent="commerce",
            safety_reason="order_id_required",
            commercial_data={
                "success": False,
                "stage": "order_payment",
                "payment": {"status": "not_available"},
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    print("[sales.payment.lookup]", {"order_id": target})
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        result = await execute("get_order_payment", {"order_id": target})
    except Exception as exc:
        result = {
            "error": "commerce_upstream_error",
            "error_type": type(exc).__name__,
        }

    if "error" in result or result.get("success") is False:
        print("[sales.payment.state]", {
            "order_id": target,
            "payment_type": None,
            "has_payment": None,
            "payment_url_present": False,
            "status": "unknown",
        })
        return AgentResult(
            reply_text="A consulta atual do pagamento nao pode ser confirmada.",
            intent="commerce",
            safety_reason="order_payment_technical_failure",
            commercial_data={
                "success": False,
                "order_id": target,
                "stage": "order_payment",
                "payment": {
                    "status": "unknown",
                    "has_payment": None,
                    "payment_url_available": False,
                },
            },
            response_metadata={
                "domain": "commerce",
                "payment_state": {
                    "order_payment_status": "unknown",
                    "order_has_payment": None,
                    "order_payment_checked_at": checked_at,
                },
                "purchase_stage": "order_created",
                "clear_pending_action": True,
                "used_tray": True,
                **_payment_failure_metadata(result),
            },
        )

    payment_payload = result.get("payment")
    payment_present = isinstance(payment_payload, dict)
    payment = payment_payload if payment_present else {}
    method_id = payment.get("method_id")
    method_id = str(method_id) if method_id is not None else None
    method = payment.get("method")
    method = str(method) if method is not None else None
    payment_type = payment.get("type")
    payment_type = str(payment_type) if payment_type is not None else None
    has_payment = payment.get("has_payment")
    has_payment = has_payment if isinstance(has_payment, bool) else None
    payment_date = payment.get("payment_date")
    payment_date = str(payment_date) if payment_date is not None else None
    raw_payment_url = payment.get("payment_url")
    payment_url = (
        raw_payment_url
        if isinstance(raw_payment_url, str) and raw_payment_url
        else None
    )
    if has_payment is True:
        status = "confirmed"
    elif has_payment is False:
        status = "pending"
    elif payment_present:
        status = "unknown"
    else:
        status = "not_available"

    facts = {
        "status": status,
        "method_id": method_id,
        "method": method,
        "type": payment_type,
        "has_payment": has_payment,
        "payment_date": payment_date,
        "payment_url": payment_url,
        "payment_url_available": payment_url is not None,
    }
    print("[sales.payment.state]", {
        "order_id": target,
        "payment_type": payment_type,
        "has_payment": has_payment,
        "payment_url_present": payment_url is not None,
        "status": status,
    })
    return AgentResult(
        reply_text="Pagamento factual do pedido consultado.",
        intent="commerce",
        commercial_data={
            "success": True,
            "order_id": str(result.get("order_id") or target),
            "payment": facts,
        },
        response_metadata={
            "domain": "commerce",
            "payment_state": {
                "order_payment_method_id": method_id,
                "order_payment_method": method,
                "order_payment_type": payment_type,
                "order_payment_url": payment_url,
                "order_payment_status": status,
                "order_has_payment": has_payment,
                "order_payment_date": payment_date,
                "order_payment_checked_at": checked_at,
            },
            "purchase_stage": (
                "payment_confirmed"
                if status == "confirmed"
                else "awaiting_payment"
                if status == "pending"
                else "order_created"
            ),
            **(
                {
                    "pending_action": "awaiting_payment",
                    "pending_action_product_ids": [],
                }
                if status == "pending"
                else {"clear_pending_action": True}
            ),
            "used_tray": True,
        },
    )
