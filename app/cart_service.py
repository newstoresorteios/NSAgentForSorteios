from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .commerce_context import (
    CommerceCartItem,
    CommerceConversationState,
    CommerceProductReference,
)
from .models import AgentResult, SalesInterpretation
from .product_retrieval import (
    commercial_availability_facts,
    product_availability_state,
    resolve_commercial_price,
)


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CartItemRequest:
    product_reference: CommerceProductReference
    quantity: int = 1
    position: int | None = None
    resolved_from: str = "context"
    variant_preferences: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedCartItem:
    product_reference: CommerceProductReference
    product: dict[str, Any]
    variant: dict[str, Any] | None
    quantity: int
    price: str
    position: int | None
    resolved_from: str


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


def _validation_failure(
    message: str,
    reason: str = "cart_validation_error",
) -> AgentResult:
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


def _flag_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return _fold_choice(value) in {"1", "true", "yes", "sim"}
    return False


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
    if result.safety_reason == "variant_required":
        result.response_metadata.setdefault("pending_action", "create_cart")
        result.response_metadata.setdefault(
            "pending_action_product_ids",
            [product_reference.product_id],
        )
        print("[sales.pending_action]", {
            "action": "create_cart",
            "has_product": True,
            "confirmation": "none",
            "executed": False,
        })
    return result


_VARIANT_NON_CHOICE_FIELDS = {
    "id",
    "variant_id",
    "product_id",
    "reference",
    "sku",
    "price",
    "promotional_price",
    "current_price",
    "stock",
    "available",
    "available_in_store",
    "available_for_purchase",
    "availability",
    "variationsettings",
    "primary_image_url",
    "primary_image",
    "image_url",
    "image",
    "images",
}


def _choice_scalars(value: Any, *, prefix: str = "") -> dict[str, str]:
    choices: dict[str, str] = {}
    if isinstance(value, dict):
        label = value.get("name") or value.get("label") or value.get("key")
        selected = value.get("value")
        if label is not None and selected not in (None, "", [], {}):
            choices[str(label)] = str(selected)
        for key, item in value.items():
            if key in {"name", "label", "key", "value"}:
                continue
            if str(key).lower() in _VARIANT_NON_CHOICE_FIELDS:
                continue
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            choices.update(_choice_scalars(item, prefix=nested_prefix))
        return choices
    if isinstance(value, list):
        for index, item in enumerate(value):
            choices.update(_choice_scalars(item, prefix=f"{prefix}.{index}"))
        return choices
    if value not in (None, "") and prefix:
        choices[prefix] = str(value)
    return choices


def variant_choices(variant: dict[str, Any]) -> dict[str, str]:
    choices: dict[str, str] = {}
    name = variant.get("name")
    value = variant.get("value")
    if name not in (None, "") and value not in (None, ""):
        choices[str(name)] = str(value)
    for key, item in variant.items():
        normalized_key = str(key).lower()
        if normalized_key in _VARIANT_NON_CHOICE_FIELDS:
            if normalized_key == "sku" and isinstance(item, (dict, list)):
                choices.update(_choice_scalars(item, prefix=str(key)))
            continue
        if key in {"name", "value"} and name not in (None, "") and value not in (None, ""):
            continue
        if isinstance(item, (dict, list)):
            choices.update(_choice_scalars(item, prefix=str(key)))
        elif item not in (None, "", True, False):
            choices[str(key)] = str(item)
    return choices


def _fold_choice(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(char)
    ).strip()


def _choice_signature(variant: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (
            _fold_choice(key),
            _fold_choice(value),
        )
        for key, value in variant_choices(variant).items()
        if str(value).strip()
    ))


def _preference_values(preferences: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in preferences.values():
        if isinstance(value, list):
            values.extend(
                _fold_choice(item)
                for item in value
                if str(item).strip()
            )
        elif value not in (None, "", {}, False):
            values.append(_fold_choice(value))
    return values


async def _resolve_variant(
    product: dict[str, Any],
    product_reference: CommerceProductReference,
    preferences: dict[str, Any],
    execute: ToolExecutor,
) -> tuple[dict[str, Any] | None, AgentResult | None]:
    selected_id = product_reference.variant_id
    requires_variation = _flag_is_true(product.get("has_variation")) or selected_id is not None
    if not requires_variation:
        print("[sales.variant.resolve]", {
            "variant_count": 0,
            "eligible_count": 0,
            "distinct_choice_count": 0,
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": False,
        })
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
        print("[sales.variant.resolve]", {
            "variant_count": len(variants),
            "eligible_count": len(variants),
            "distinct_choice_count": len({_choice_signature(variant) for variant in variants}),
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": True,
        })
        return selected, None

    eligible = [
        variant
        for variant in variants
        if product_availability_state(variant) != "unavailable"
    ]
    signatures = {_choice_signature(variant) for variant in eligible}
    selected: dict[str, Any] | None = None
    choice_required = False
    if not variants:
        selected = None
    elif not eligible:
        print("[sales.variant.resolve]", {
            "variant_count": len(variants),
            "eligible_count": 0,
            "distinct_choice_count": 0,
            "choice_required": False,
            "auto_selected": False,
            "has_variant_id": False,
        })
        return None, _validation_failure(
            "As variações deste produto estão indisponíveis no momento.",
            "product_unavailable",
        )
    elif len(eligible) == 1 or len(signatures) <= 1:
        selected = eligible[0]
    else:
        preference_values = _preference_values(preferences)
        matched = [
            variant
            for variant in eligible
            if any(
                preference in " ".join(
                    f"{key} {value}"
                    for key, value in _choice_signature(variant)
                )
                for preference in preference_values
            )
        ]
        matched_signatures = {_choice_signature(variant) for variant in matched}
        if matched and len(matched_signatures) == 1:
            selected = matched[0]
        else:
            choice_required = True
    print("[sales.variant.resolve]", {
        "variant_count": len(variants),
        "eligible_count": len(eligible),
        "distinct_choice_count": len(signatures),
        "choice_required": choice_required,
        "auto_selected": selected is not None,
        "has_variant_id": bool(selected and _variant_id(selected)),
    })
    if not choice_required:
        return selected, None
    choice_labels = [
        " / ".join(
            f"{key}: {value}"
            for key, value in variant_choices(variant).items()
        )
        for variant in eligible[:10]
    ]
    variant_reply = "Preciso confirmar uma das opções reais deste produto."
    if any(choice_labels):
        variant_reply += "\n" + "\n".join(
            f"{index}. {label}"
            for index, label in enumerate(choice_labels, start=1)
            if label
        )
    return None, AgentResult(
        reply_text=variant_reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="variant_required",
        commercial_data={
            "cart": {"status": "variant_required"},
            "variants": [
                {
                    **variant,
                    "choices": variant_choices(variant),
                }
                for variant in eligible[:10]
            ],
            "products": [product],
        },
        response_metadata={"used_tray": True},
    )


def _price_for_cart(
    product: dict[str, Any],
    variant: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    variant_resolution = resolve_commercial_price(
        variant or {},
        require_positive=True,
    )
    product_resolution = resolve_commercial_price(
        product,
        require_positive=True,
    )
    selected = (
        variant_resolution
        if variant_resolution.amount is not None
        else product_resolution
    )
    valid = selected.amount is not None and selected.amount > Decimal("0")
    print("[sales.cart.price]", {
        "product_id_present": bool(product.get("id") or product.get("product_id")),
        "price_source": selected.source,
        "price_valid": valid,
    })
    if not valid:
        return None, selected.source
    return format(selected.amount.quantize(Decimal("0.01")), "f"), selected.source


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
                "items": [
                    item.model_dump(mode="json")
                    for item in state.cart_items
                ],
            }
        },
        response_metadata={
            "purchase_stage": "cart_created",
            "used_tray": False,
        },
    )


async def _prepare_item(
    request: CartItemRequest,
    execute: ToolExecutor,
) -> tuple[_PreparedCartItem | None, AgentResult | None]:
    reference = request.product_reference
    if isinstance(request.quantity, bool) or request.quantity < 1:
        return None, _with_selected_product(
            _validation_failure("Informe uma quantidade válida para eu preparar o carrinho."),
            reference,
        )

    current = await execute("get_product", {"product_id": reference.product_id})
    if "error" in current:
        return None, _with_selected_product(
            _technical_failure(current.get("status_code")),
            reference,
        )
    product = {
        key: value
        for key, value in {
            "id": reference.product_id,
            "name": reference.name,
            "reference": reference.reference,
            "ean": reference.ean,
            "brand": reference.brand,
        }.items()
        if value is not None
    }
    product.update(current)
    product["commercial_availability"] = commercial_availability_facts(product)
    print("[sales.availability.fact]", {
        "has_stock": product["commercial_availability"]["has_stock"],
        "has_lead_time": product["commercial_availability"]["has_lead_time"],
        "immediate_delivery_supported": product["commercial_availability"]["immediate_delivery_supported"],
    })

    if product_availability_state(product) == "unavailable":
        return None, _with_selected_product(
            _validation_failure(
                "Esse produto está indisponível no momento, então não foi adicionado ao carrinho.",
                "product_unavailable",
            ),
            reference,
        )

    variant, variant_error = await _resolve_variant(
        product,
        reference,
        request.variant_preferences,
        execute,
    )
    if variant_error is not None:
        print("[sales.purchase.progress]", {
            "purchase_stage": "selection",
            "blocking_reason": variant_error.safety_reason,
        })
        return None, _with_selected_product(variant_error, reference)
    if variant is not None and product_availability_state(variant) == "unavailable":
        return None, _with_selected_product(
            _validation_failure(
                "A variação escolhida está indisponível no momento.",
                "product_unavailable",
            ),
            reference,
        )

    price, _price_source = _price_for_cart(product, variant)
    if price is None:
        return None, _with_selected_product(
            _validation_failure(
                "Não consegui validar o preço atual desse produto para criar o carrinho.",
            ),
            reference,
        )

    active_reference = CommerceProductReference(
        product_id=str(product.get("id") or reference.product_id),
        reference=str(product["reference"]) if product.get("reference") is not None else reference.reference,
        variant_id=_variant_id(variant) if variant is not None else None,
        name=str(product["name"]) if product.get("name") is not None else reference.name,
        ean=str(product["ean"]) if product.get("ean") is not None else reference.ean,
        brand=str(product["brand"]) if product.get("brand") is not None else reference.brand,
    )
    print("[sales.purchase.progress]", {
        "purchase_stage": "cart_creating",
        "blocking_reason": None,
    })
    return _PreparedCartItem(
        product_reference=active_reference,
        product=product,
        variant=variant,
        quantity=request.quantity,
        price=price,
        position=request.position,
        resolved_from=request.resolved_from,
    ), None


def _verified_items(
    complete: dict[str, Any],
) -> list[CommerceCartItem]:
    parsed: list[CommerceCartItem] = []
    for item in complete.get("items", []):
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        quantity = item.get("quantity")
        try:
            if product_id is not None:
                parsed.append(CommerceCartItem(
                    product_id=str(product_id),
                    variant_id=(
                        str(item["variant_id"])
                        if item.get("variant_id") is not None
                        else None
                    ),
                    quantity=int(quantity or 1),
                ))
        except (TypeError, ValueError):
            continue
    return parsed


def _cart_state(
    *,
    cart: dict[str, Any],
    session_id: str,
    cart_url: str,
    items: list[CommerceCartItem],
) -> dict[str, Any]:
    last = items[-1] if items else None
    return {
        "cart_id": str(cart["cart_id"]) if cart.get("cart_id") is not None else None,
        "cart_session_id": session_id,
        "cart_url": cart_url,
        "cart_product_id": last.product_id if last else None,
        "cart_variant_id": last.variant_id if last else None,
        "cart_quantity": last.quantity if last else None,
        "cart_items": [item.model_dump(mode="json") for item in items],
    }


async def create_cart_items_checkout(
    *,
    item_requests: list[CartItemRequest],
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    print("[sales.cart.items]", {
        "requested_count": len(item_requests),
        "resolved_count": len(item_requests),
    })
    if not item_requests:
        return _validation_failure(
            "Não consegui identificar quais produtos devem entrar no carrinho.",
            "cart_validation_error",
        )

    expected = sorted(
        (
            request.product_reference.product_id,
            request.product_reference.variant_id,
            request.quantity,
        )
        for request in item_requests
    )
    existing = sorted(
        (item.product_id, item.variant_id, item.quantity)
        for item in state.cart_items
    )
    if (
        state.cart_session_id
        and _valid_cart_url(state.cart_url)
        and expected
        and (
            expected == existing
            or (
                not existing
                and len(expected) == 1
                and state.cart_product_id == expected[0][0]
                and state.cart_variant_id == expected[0][1]
                and state.cart_quantity == expected[0][2]
            )
        )
    ):
        return current_cart_reply(state, checkout_question=False)

    prepared: list[_PreparedCartItem] = []
    for request in item_requests:
        item, error = await _prepare_item(request, execute)
        print("[sales.cart.item]", {
            "position": request.position,
            "has_product_id": bool(request.product_reference.product_id),
            "quantity": request.quantity,
            "status": error.safety_reason if error else "validated",
        })
        if error is not None:
            return error
        if item is not None:
            prepared.append(item)

    session_id = state.cart_session_id
    cart_url = _valid_cart_url(state.cart_url)
    cart: dict[str, Any] = {}
    successful = [
        CommerceCartItem.model_validate(item)
        for item in state.cart_items
    ]
    failed_item: _PreparedCartItem | None = None

    for item in prepared:
        payload = {
            "product_id": item.product_reference.product_id,
            "variant_id": item.product_reference.variant_id,
            "quantity": item.quantity,
            "price": item.price,
        }
        if session_id:
            payload["session_id"] = session_id
        created = await execute("create_cart", payload)
        if "error" in created:
            failed_item = item
            print("[sales.cart.item]", {
                "position": item.position,
                "has_product_id": True,
                "quantity": item.quantity,
                "status": "cart_technical_failure",
            })
            break
        cart = created
        session_id = str(created["session_id"]) if created.get("session_id") is not None else session_id
        cart_url = _valid_cart_url(created.get("cart_url")) or cart_url
        successful.append(CommerceCartItem(
            product_id=item.product_reference.product_id,
            variant_id=item.product_reference.variant_id,
            quantity=item.quantity,
        ))
        print("[sales.cart.item]", {
            "position": item.position,
            "has_product_id": True,
            "quantity": item.quantity,
            "status": "added",
        })

    print("[sales.cart.create]", {
        "success": failed_item is None and bool(session_id and cart_url),
        "has_session_id": bool(session_id),
        "has_cart_url": bool(cart_url),
    })
    if not session_id or not cart_url:
        return _technical_failure()

    complete = await execute("get_cart_complete", {"session_id": session_id})
    verify_ok = "error" not in complete
    verified_items = (
        _verified_items(complete)
        if verify_ok
        else [
            CommerceCartItem.model_validate(item)
            for item in state.cart_items
        ]
    )
    print("[sales.cart.verify]", {
        "item_count": len(verified_items),
        "has_total": bool(
            verify_ok
            and any(complete.get(key) is not None for key in ("total", "current_total", "subtotal"))
        ),
    })
    verification_matches = verify_ok and all(
        any(
            verified.product_id == item.product_reference.product_id
            and (
                item.product_reference.variant_id is None
                or verified.variant_id == item.product_reference.variant_id
            )
            and verified.quantity >= item.quantity
            for verified in verified_items
        )
        for item in prepared
        if item is not failed_item
    )

    cart_state = _cart_state(
        cart=cart,
        session_id=session_id,
        cart_url=cart_url,
        items=verified_items,
    )
    active = prepared[-1].product_reference
    partial = failed_item is not None or not verification_matches
    status = "cart_partial_failure" if partial else "cart_created"
    reply = (
        "Parte dos itens foi adicionada ao carrinho. Confira o estado atual no checkout oficial."
        if partial
        else "Carrinho atualizado com sucesso. Finalize pelo checkout oficial."
    )
    print("[sales.cart.state]", {
        "purchase_stage": "cart_created",
        "has_cart_session": True,
    })
    return AgentResult(
        reply_text=f"{reply}\n{cart_url}",
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_partial_failure" if partial else None,
        commercial_data={
            "products": [item.product for item in prepared],
            "items": [
                {
                    "product_id": item.product_reference.product_id,
                    "variant_id": item.product_reference.variant_id,
                    "quantity": item.quantity,
                    "current_price": item.price,
                }
                for item in prepared
            ],
            "cart": {
                "status": status,
                "cart_id": cart_state["cart_id"],
                "session_id": session_id,
                "cart_url": cart_url,
                "items": [item.model_dump(mode="json") for item in verified_items],
                "total": complete.get("total") or complete.get("current_total"),
                "verification_ok": verify_ok,
            },
            **(
                {
                    "variant": prepared[0].variant,
                    "quantity": prepared[0].quantity,
                    "current_price": prepared[0].price,
                }
                if len(prepared) == 1
                else {}
            ),
        },
        response_metadata={
            "domain": "commerce",
            "active_product": active.model_dump(mode="json"),
            "purchase_stage": "cart_created",
            "cart_state": cart_state,
            "used_tray": True,
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
    return await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=product_reference,
                quantity=quantity,
                position=interpretation.reference_position,
                resolved_from=interpretation.reference_type or "context",
                variant_preferences=interpretation.preferences.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            )
        ],
        state=state,
        execute=execute,
    )
