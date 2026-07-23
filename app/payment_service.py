from __future__ import annotations

from typing import Any, Awaitable, Callable

from .commerce_context import CommerceConversationState
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


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
        commercial_data={"cart": cart},
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
    if payment_method_preference == "pix":
        method_available = isinstance(options.get("pix"), dict)
    elif payment_method_preference == "card":
        method_available = (
            isinstance(options.get("card"), dict)
            or bool(installments)
        )
    elif payment_method_preference == "boleto":
        method_available = isinstance(options.get("boleto"), dict)
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
        "requested_method_available": method_available,
        "requested_installment_count": installment_count,
        "requested_installment": selected,
        "cart_url": state.cart_url,
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
            if payment_method_preference is not None and method_available is False
            else None
        ),
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
            "used_tray": True,
        },
    )
