from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .commerce_context import CommerceConversationState, CommerceProductReference
from .models import AgentResult, SalesInterpretation
from .product_retrieval import effective_price, product_availability_state


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _technical_failure(status_code: int | None = None) -> AgentResult:
    print("[sales.cart.error]", {
        "error_type": "cart_technical_failure",
        "status_code": status_code,
    })
    return AgentResult(
        reply_text=(
            "Não consegui preparar o carrinho neste momento. "
            "Tente novamente em instantes."
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_technical_failure",
        response_metadata={"used_tray": True},
    )


def _validation_failure(message: str, reason: str = "cart_validation_error") -> AgentResult:
    print("[sales.cart.error]", {
        "error_type": reason,
        "status_code": None,
    })
    return AgentResult(
        reply_text=message,
        intent="commerce",
        handoff_required=False,
        safety_reason=reason,
    )


def _price_string(product: dict[str, Any]) -> str | None:
    value = effective_price(product)
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, TypeError, ValueError):
        return None


def _valid_cart_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _variant_id(variant: dict[str, Any]) -> str | None:
    value = variant.get("variant_id") or variant.get("id")
    return str(value) if value is not None else None


def _with_selected_product(
    result: AgentResult,
    product_reference: CommerceProductReference,
) -> AgentResult:
    result.response_metadata.setdefault("domain", "commerce")
    result.response_metadata.setdefault(
        "active_product",
        product_reference.model_dump(mode="json"),
    )
    result.response_metadata.setdefault("purchase_stage", "selection")
    return result


async def _resolve_variant(
    product: dict[str, Any],
    product_reference: CommerceProductReference,
    execute: ToolExecutor,
) -> tuple[dict[str, Any] | None, AgentResult | None]:
    selected_id = product_reference.variant_id
    requires_variation = bool(product.get("has_variation")) or selected_id is not None
    if not requires_variation:
        return None, None

    result = await execute(
        "list_product_variants",
        {"product_id": product_reference.product_id},
    )
    if "error" in result:
        return None, _technical_failure(result.get("status_code"))
    variants = [
        variant
        for variant in result.get("variants", [])
        if isinstance(variant, dict) and _variant_id(variant)
    ]

    if selected_id is not None:
        selected = next(
            (
                variant
                for variant in variants
                if _variant_id(variant) == str(selected_id)
            ),
            None,
        )
        if selected is None:
            return None, _validation_failure(
                "Não consegui validar a variação escolhida. Escolha uma das opções disponíveis.",
                "variant_required",
            )
        return selected, None

    eligible = [
        variant
        for variant in variants
        if product_availability_state(variant) != "unavailable"
    ]
    if len(eligible) == 1:
        return eligible[0], None
    return None, AgentResult(
        reply_text="Preciso confirmar qual variação você prefere antes de preparar o carrinho.",
        intent="commerce",
        handoff_required=False,
        safety_reason="variant_required",
        commercial_data={
            "cart": {"status": "variant_required"},
            "variants": eligible[:10],
            "products": [product],
        },
        response_metadata={"used_tray": True},
    )


def current_cart_reply(
    state: CommerceConversationState,
    *,
    checkout_question: bool,
) -> AgentResult:
    cart_url = _valid_cart_url(state.cart_url)
    if not cart_url or not state.cart_session_id:
        return _validation_failure(
            "Ainda não há um carrinho ativo nesta conversa.",
        )
    message = (
        "O pagamento é concluído com segurança no checkout oficial da loja."
        if checkout_question
        else "Este é o link do seu carrinho atual."
    )
    return AgentResult(
        reply_text=f"{message}\n{cart_url}",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "cart": {
                "status": "cart_ready",
                "cart_url": cart_url,
                "quantity": state.cart_quantity,
            }
        },
        response_metadata={
            "purchase_stage": "cart_created",
            "used_tray": False,
        },
    )


async def create_cart_checkout(
    *,
    interpretation: SalesInterpretation,
    product_reference: CommerceProductReference,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    quantity = interpretation.quantity or 1
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return _with_selected_product(
            _validation_failure("Informe uma quantidade válida para eu preparar o carrinho."),
            product_reference,
        )

    print("[sales.cart.resolve]", {
        "has_active_product": state.active_product is not None,
        "resolved_from": (
            "list_position"
            if interpretation.reference_type == "list_position"
            else "context"
        ),
        "has_variant": product_reference.variant_id is not None,
        "quantity": quantity,
        "purchase_stage": state.purchase_stage,
    })

    if (
        state.cart_session_id
        and _valid_cart_url(state.cart_url)
        and state.cart_product_id == product_reference.product_id
        and state.cart_variant_id == product_reference.variant_id
        and state.cart_quantity == quantity
    ):
        return current_cart_reply(state, checkout_question=False)

    current = await execute(
        "get_product",
        {"product_id": product_reference.product_id},
    )
    if "error" in current:
        return _with_selected_product(
            _technical_failure(current.get("status_code")),
            product_reference,
        )
    product = {
        key: value
        for key, value in {
            "id": product_reference.product_id,
            "name": product_reference.name,
            "reference": product_reference.reference,
            "ean": product_reference.ean,
            "brand": product_reference.brand,
        }.items()
        if value is not None
    }
    product.update(current)

    availability_state = product_availability_state(product)
    if availability_state == "unavailable":
        return _with_selected_product(
            _validation_failure(
                "Esse produto está indisponível no momento, então não criei o carrinho.",
                "product_unavailable",
            ),
            product_reference,
        )

    variant, variant_error = await _resolve_variant(product, product_reference, execute)
    if variant_error is not None:
        return _with_selected_product(variant_error, product_reference)
    if variant is not None and product_availability_state(variant) == "unavailable":
        return _with_selected_product(
            _validation_failure(
                "A variação escolhida está indisponível no momento.",
                "product_unavailable",
            ),
            product_reference,
        )

    price_source = variant if variant is not None and effective_price(variant) is not None else product
    price = _price_string(price_source)
    if price is None:
        return _with_selected_product(
            _validation_failure(
                "Não consegui validar o preço atual desse produto para criar o carrinho.",
            ),
            product_reference,
        )

    resolved_variant_id = _variant_id(variant) if variant is not None else None
    cart = await execute(
        "create_cart",
        {
            "product_id": product_reference.product_id,
            "variant_id": resolved_variant_id,
            "quantity": quantity,
            "price": price,
        },
    )
    if "error" in cart:
        print("[sales.cart.create]", {
            "success": False,
            "has_session_id": False,
            "has_cart_url": False,
        })
        return _with_selected_product(
            _technical_failure(cart.get("status_code")),
            product_reference,
        )

    session_id = cart.get("session_id")
    cart_url = _valid_cart_url(cart.get("cart_url"))
    if session_id is None or cart_url is None:
        print("[sales.cart.create]", {
            "success": False,
            "has_session_id": session_id is not None,
            "has_cart_url": cart_url is not None,
        })
        return _with_selected_product(
            _validation_failure(
                "O carrinho foi processado, mas o checkout não retornou um link válido.",
            ),
            product_reference,
        )

    active_product = CommerceProductReference(
        product_id=str(product.get("id") or product_reference.product_id),
        reference=(
            str(product["reference"])
            if product.get("reference") is not None
            else product_reference.reference
        ),
        variant_id=resolved_variant_id,
        name=(
            str(product["name"])
            if product.get("name") is not None
            else product_reference.name
        ),
        ean=(
            str(product["ean"])
            if product.get("ean") is not None
            else product_reference.ean
        ),
        brand=(
            str(product["brand"])
            if product.get("brand") is not None
            else product_reference.brand
        ),
    )
    cart_state = {
        "cart_id": str(cart["cart_id"]) if cart.get("cart_id") is not None else None,
        "cart_session_id": str(session_id),
        "cart_url": cart_url,
        "cart_product_id": active_product.product_id,
        "cart_variant_id": resolved_variant_id,
        "cart_quantity": quantity,
    }
    print("[sales.cart.create]", {
        "success": True,
        "has_session_id": True,
        "has_cart_url": True,
    })
    print("[sales.cart.state]", {
        "purchase_stage": "cart_created",
        "has_cart_session": True,
    })
    return AgentResult(
        reply_text=f"Carrinho criado com sucesso. Finalize pelo checkout oficial: {cart_url}",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "products": [product],
            "variant": variant,
            "quantity": quantity,
            "current_price": price,
            "cart": {
                "status": "cart_created",
                "cart_id": cart_state["cart_id"],
                "session_id": cart_state["cart_session_id"],
                "cart_url": cart_url,
            },
        },
        response_metadata={
            "domain": "commerce",
            "active_product": active_product.model_dump(mode="json"),
            "purchase_stage": "cart_created",
            "cart_state": cart_state,
            "used_tray": True,
        },
    )
