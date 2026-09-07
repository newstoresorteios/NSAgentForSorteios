from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from app.commerce.checkout_data_service import checkout_data_template
from app.commerce.commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceConversationState,
    checkout_fields_view,
    checkout_missing_fields,
)
from app.config import get_settings
from app.models import AgentResult


CheckoutChannel = Literal["whatsapp", "site"]

# The agent can create the Tray order after an explicit review.
# Native PIX in chat is gated by PIX_DIRECT_ENABLED + MP token.
WHATSAPP_ORDER_SUPPORTED = True
WHATSAPP_HOSTED_PAYMENT_SUPPORTED = True
WHATSAPP_PAYMENT_SUPPORTED = False  # card/tokenization still outside this backend
WHATSAPP_CHECKOUT_SUPPORTED = False

_CART_PAY_STAGES = frozenset(
    {
        "cart_created",
        "checkout_ready",
        "checkout_channel_selection",
    }
)
_CART_PAY_STATUSES = frozenset({"cart_created", "cart_ready"})
_SKIP_CART_PAY_STAGES = frozenset(
    {
        "payment_discussion",
        "shipping",
        "checkout_data",
        "order_review",
        "order_created",
        "awaiting_order_confirmation",
    }
)


def _site_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    return candidate


def official_cart_url(value: str | None) -> str | None:
    return _site_url(value)


def visible_cart_url(
    state: CommerceConversationState | None = None,
    *,
    selected_channel: CheckoutChannel | None = None,
    cart_url: str | None = None,
) -> str | None:
    """Site checkout URL the customer may receive. Hidden on WhatsApp order."""
    channel = selected_channel
    if channel is None and state is not None:
        channel = state.checkout_channel_preference
    if channel == "whatsapp":
        return None
    raw = cart_url
    if raw is None and state is not None:
        raw = state.cart_url
    return _site_url(raw)


def _display_prices(
    products: list[dict[str, Any]] | None,
) -> tuple[float | None, float | None]:
    if not products:
        return None, None
    product = next((item for item in products if isinstance(item, dict)), None)
    if product is None:
        return None, None
    from app.commerce.commerce_router import (
        _list_price,
        _payment_details,
        _pix_cash_price,
    )

    payment = _payment_details(product)
    return _list_price(product), _pix_cash_price(product, payment)


def cart_pay_link_copy(
    *,
    cart_url: str,
    products: list[dict[str, Any]] | None = None,
) -> str:
    """Customer copy after a live reserved cart: open the official link and pay."""
    url = cart_url.strip()
    lines = [
        "Separei o relógio no carrinho — já está reservado.",
        "Entra neste link e paga para concluir:",
        url,
    ]
    list_price, pix_price = _display_prices(products)
    from app.commerce.commerce_router import _price_label

    list_label = _price_label(list_price) if list_price is not None else None
    pix_label = _price_label(pix_price) if pix_price is not None else None
    if (
        list_label
        and pix_label
        and list_price is not None
        and pix_price is not None
        and abs(list_price - pix_price) >= 0.01
    ):
        lines.append(f"No site ele aparece a {list_label}, ou {pix_label} no PIX.")
    elif list_label:
        lines.append(f"No site ele aparece a {list_label}.")
    return "\n".join(lines)


def apply_live_cart_pay_copy(
    result: AgentResult,
    *,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    """Replace thin/LLM cart copy with pay-the-link when a site cart URL is live."""
    if result.safety_reason:
        return None
    data = result.commercial_data or {}
    if data.get("payment_method") or data.get("hosted_payment") or data.get("input_template"):
        return None
    cart = data.get("cart") if isinstance(data.get("cart"), dict) else {}
    checkout = data.get("checkout") if isinstance(data.get("checkout"), dict) else {}
    meta = result.response_metadata or {}
    cart_state = meta.get("cart_state") if isinstance(meta.get("cart_state"), dict) else {}
    stage = str(meta.get("purchase_stage") or "")
    status = str(cart.get("status") or "")
    if status == "cart_partial_failure" or stage in _SKIP_CART_PAY_STAGES:
        return None
    has_cart_signal = bool(
        status in _CART_PAY_STATUSES
        or stage in _CART_PAY_STAGES
        or checkout.get("cart_ready")
        or cart_state.get("cart_url")
        or cart_state.get("cart_session_id")
    )
    if not has_cart_signal:
        return None
    if commerce_state is not None and commerce_state.checkout_channel_preference == "whatsapp":
        return None
    url = None
    for candidate in (
        cart.get("cart_url"),
        checkout.get("cart_url"),
        cart_state.get("cart_url"),
        getattr(commerce_state, "cart_url", None),
    ):
        url = official_cart_url(candidate)
        if url:
            break
    if not url:
        return None
    products = [
        item
        for item in (data.get("products") or [])
        if isinstance(item, dict)
    ]
    copy = cart_pay_link_copy(cart_url=url, products=products)
    updated = result.model_copy(deep=True)
    updated.reply_text = copy
    updated.handoff_required = False
    return updated


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
    official_url = _site_url(state.cart_url)
    cart_ready = bool(state.cart_session_id and official_url)
    settings = get_settings()
    pix_direct = bool(
        cart_ready
        and settings.pix_direct_enabled
        and settings.resolved_mp_access_token()
    )
    supported = {
        "whatsapp": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "site": bool(cart_ready and official_url),
    }
    facts = {
        "cart_ready": cart_ready,
        "whatsapp_checkout_supported": bool(cart_ready and WHATSAPP_CHECKOUT_SUPPORTED),
        "whatsapp_order_supported": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "whatsapp_hosted_payment_supported": bool(
            cart_ready and WHATSAPP_HOSTED_PAYMENT_SUPPORTED
        ),
        "whatsapp_native_payment_supported": pix_direct,
        "whatsapp_payment_supported": bool(cart_ready and WHATSAPP_PAYMENT_SUPPORTED),
        "pix_direct_enabled": pix_direct,
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
    public_url = visible_cart_url(state, selected_channel=selected_channel)
    if public_url:
        facts["cart_url"] = public_url
    return facts


def checkout_channel_choice_prompt(
    state: CommerceConversationState,
) -> str:
    """Deterministic copy when a reserved cart is ready: enter the link and pay."""
    public_url = visible_cart_url(state)
    if public_url:
        products: list[dict[str, Any]] = []
        active = state.active_product
        if active is not None:
            dumped = (
                active.model_dump(mode="json")
                if hasattr(active, "model_dump")
                else active
            )
            if isinstance(dumped, dict):
                products.append(dumped)
        return cart_pay_link_copy(cart_url=public_url, products=products)
    facts = checkout_capabilities(state)
    if facts.get("whatsapp_order_supported"):
        return "Seu carrinho está pronto. Prefere fechar por aqui no WhatsApp?"
    return "Seu carrinho está pronto. Posso te enviar o link para concluir pelo site."


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
    missing_checkout_fields = (
        checkout_missing_fields(state.checkout_draft)
        if supported and channel == "whatsapp"
        else []
    )
    whatsapp_needs_data = bool(missing_checkout_fields)
    whatsapp_needs_zipcode = bool(
        supported
        and channel == "whatsapp"
        and not whatsapp_needs_data
        and not state.shipping_quotes
        and not state.selected_shipping
    )
    if whatsapp_needs_data:
        print("[sales.checkout.next_requirement]", {
            "purchase_stage": "checkout_data",
            "pending_action": "awaiting_checkout_data",
            "missing_fields": missing_checkout_fields,
        })
    elif whatsapp_needs_zipcode:
        print("[sales.checkout.next_requirement]", {
            "purchase_stage": "shipping",
            "pending_action": "awaiting_shipping_zipcode",
            "blocker_codes": ["shipping_zipcode_missing"],
        })
    template = checkout_data_template(missing_checkout_fields)
    site_url = facts.get("cart_url") if isinstance(facts.get("cart_url"), str) else None
    if channel == "site" and site_url and supported and not template:
        reply_text = cart_pay_link_copy(cart_url=site_url)
    elif template:
        reply_text = template
    elif supported:
        reply_text = (
            "Perfeito — seguimos por aqui no WhatsApp. "
            "Me diga se prefere PIX, cartão ou boleto."
        )
    else:
        reply_text = (
            "O canal solicitado ainda não possui suporte técnico para concluir esta compra."
        )
    return AgentResult(
        reply_text=reply_text,
        intent="commerce",
        handoff_required=False,
        safety_reason=None if supported else "checkout_channel_unavailable",
        commercial_data={
            "checkout": facts,
            "checkout_fields": checkout_fields_view(state.checkout_draft),
            "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
            "missing_fields": missing_checkout_fields,
            "input_template": template or None,
            "cart": {
                "status": "cart_ready",
                "items": [
                    item.model_dump(mode="json")
                    for item in state.cart_items
                ],
                **(
                    {"cart_url": facts["cart_url"]}
                    if "cart_url" in facts else {}
                ),
            }
        },
        response_metadata={
            "domain": "commerce",
            "checkout_channel_preference": channel,
            "purchase_stage": (
                "checkout_data"
                if whatsapp_needs_data
                else "shipping"
                if whatsapp_needs_zipcode
                else "checkout_ready"
                if supported
                else "checkout_channel_selection"
            ),
            "clear_pending_action": (
                supported
                and not whatsapp_needs_data
                and not whatsapp_needs_zipcode
            ),
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if not supported
                else {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if whatsapp_needs_data
                else {
                    "pending_action": "awaiting_shipping_zipcode",
                    "pending_action_product_ids": [],
                }
                if whatsapp_needs_zipcode
                else {}
            ),
            "used_tray": False,
        },
    )
