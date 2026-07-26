from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from .commerce_context import CommerceConversationState
from .models import AgentResult


CheckoutChannel = Literal["whatsapp", "site"]

# The agent can now create the Tray order after an explicit review, but payment
# execution/tokenization remains outside this backend.
WHATSAPP_ORDER_SUPPORTED = True
WHATSAPP_HOSTED_PAYMENT_SUPPORTED = True
WHATSAPP_NATIVE_PAYMENT_SUPPORTED = False
WHATSAPP_PAYMENT_SUPPORTED = False
WHATSAPP_CHECKOUT_SUPPORTED = False


def _site_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    return candidate


def checkout_capabilities(
    state: CommerceConversationState,
    *,
    selected_channel: CheckoutChannel | None = None,
) -> dict:
    effective_channel = (
        selected_channel
        if selected_channel is not None
        else state.checkout_channel_preference
    )
    official_cart_url = _site_url(state.cart_url)
    cart_ready = bool(state.cart_session_id and official_cart_url)
    supported = {
        "whatsapp": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "site": bool(cart_ready and official_cart_url),
    }
    return {
        "cart_ready": cart_ready,
        "cart_url": official_cart_url if effective_channel == "site" else None,
        "whatsapp_checkout_supported": bool(cart_ready and WHATSAPP_CHECKOUT_SUPPORTED),
        "whatsapp_order_supported": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "whatsapp_hosted_payment_supported": bool(
            cart_ready and WHATSAPP_HOSTED_PAYMENT_SUPPORTED
        ),
        "whatsapp_native_payment_supported": bool(
            cart_ready and WHATSAPP_NATIVE_PAYMENT_SUPPORTED
        ),
        "whatsapp_payment_supported": bool(cart_ready and WHATSAPP_PAYMENT_SUPPORTED),
        "site_checkout_supported": supported["site"],
        "requires_channel_choice": bool(
            cart_ready
            and (
                effective_channel is None
                or not supported[effective_channel]
            )
        ),
        "selected_channel": effective_channel,
        "selected_channel_supported": (
            supported[effective_channel]
            if effective_channel is not None
            else None
        ),
        "sensitive_payment_data_allowed_in_chat": False,
    }


def select_checkout_channel(
    state: CommerceConversationState,
    channel: CheckoutChannel,
) -> AgentResult:
    facts = checkout_capabilities(state, selected_channel=channel)
    if not facts["cart_ready"]:
        return AgentResult(
            reply_text="Ainda não há um carrinho pronto para escolher o canal de checkout.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_validation_error",
            commercial_data={"checkout": facts},
            response_metadata={"domain": "commerce"},
        )

    supported = bool(facts["selected_channel_supported"])
    return AgentResult(
        reply_text=(
            "Canal de checkout registrado."
            if supported
            else "O canal solicitado ainda não possui suporte técnico para concluir esta compra."
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason=None if supported else "checkout_channel_unavailable",
        commercial_data={
            "checkout": facts,
            "cart": {
                "status": "cart_ready",
                "cart_url": facts["cart_url"],
                "items": [
                    item.model_dump(mode="json")
                    for item in state.cart_items
                ],
            },
        },
        response_metadata={
            "domain": "commerce",
            "checkout_channel_preference": channel,
            "purchase_stage": (
                "checkout_ready"
                if supported
                else "checkout_channel_selection"
            ),
            "clear_pending_action": supported,
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if not supported
                else {}
            ),
            "used_tray": False,
        },
    )
