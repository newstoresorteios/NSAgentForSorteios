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
    execute: ToolExecutor,
) -> AgentResult:
    if not state.cart_session_id:
        return _no_cart()
    result = await execute(
        "get_payment_options",
        {"cart_session_id": state.cart_session_id},
    )
    if "error" in result:
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
    print("[sales.payment.options]", {
        "has_session_id": True,
        "option_count": len(installments) + int("pix" in options),
    })
    facts: dict[str, Any] = {
        "payment_options": options,
        "requested_installment_count": installment_count,
        "requested_installment": selected,
        "cart_url": state.cart_url,
    }
    if installment_count is not None and selected is None:
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
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
            "used_tray": True,
        },
    )
