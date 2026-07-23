from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import AgentResult, PurchaseItem, SalesInterpretation


class CommerceProductReference(BaseModel):
    product_id: str
    reference: str | None = None
    variant_id: str | None = None
    name: str | None = None
    ean: str | None = None
    brand: str | None = None


class PresentedCommerceProduct(CommerceProductReference):
    position: int


class CommerceCartItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(ge=1)


class CommerceConversationState(BaseModel):
    active_domain: Literal["commerce", "raffle"] | None = None
    active_topic: str | None = None
    active_product: CommerceProductReference | None = None
    last_presented_products: list[PresentedCommerceProduct] = Field(default_factory=list)
    active_preferences: dict[str, Any] = Field(default_factory=dict)
    purchase_stage: str | None = None
    cart_id: str | None = None
    cart_session_id: str | None = None
    cart_url: str | None = None
    cart_product_id: str | None = None
    cart_variant_id: str | None = None
    cart_quantity: int | None = None
    cart_items: list[CommerceCartItem] = Field(default_factory=list)
    pending_action: Literal[
        "send_product_link",
        "create_cart",
        "show_images",
        "show_payment_options",
        "confirm_purchase",
    ] | None = None
    pending_action_product_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: Any) -> "CommerceConversationState":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return cls()
        try:
            return cls.model_validate(value)
        except (TypeError, ValueError):
            return cls()

    def interpreter_payload(self) -> dict[str, Any]:
        """Expose semantic identity without asking the model to handle internal IDs."""
        active = self.active_product
        return {
            "active_domain": self.active_domain,
            "active_topic": self.active_topic,
            "active_product": (
                {
                    "name": active.name,
                    "reference": active.reference,
                    "ean": active.ean,
                    "brand": active.brand,
                }
                if active
                else None
            ),
            "last_presented_products": [
                {
                    "position": product.position,
                    "name": product.name,
                    "reference": product.reference,
                    "brand": product.brand,
                }
                for product in self.last_presented_products
            ],
            "active_preferences": self.active_preferences,
            "purchase_stage": self.purchase_stage,
            "has_cart": bool(self.cart_session_id and self.cart_url),
            "cart_item_count": len(self.cart_items),
            "pending_action": self.pending_action,
            "pending_action_product_count": len(self.pending_action_product_ids),
        }


def _fold(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").lower())
        if not unicodedata.combining(char)
    ).strip()


def product_reference_from_product(product: dict[str, Any]) -> CommerceProductReference | None:
    product_id = product.get("id") or product.get("product_id")
    if product_id is None:
        return None
    return CommerceProductReference(
        product_id=str(product_id),
        reference=str(product["reference"]) if product.get("reference") is not None else None,
        variant_id=str(product["variant_id"]) if product.get("variant_id") is not None else None,
        name=str(product["name"]) if product.get("name") is not None else None,
        ean=str(product["ean"]) if product.get("ean") is not None else None,
        brand=str(product["brand"]) if product.get("brand") is not None else None,
    )


def _explicit_product_match(
    interpretation: SalesInterpretation,
    products: list[PresentedCommerceProduct],
) -> CommerceProductReference | None:
    subject = interpretation.subject
    expected_reference = _fold(subject.reference)
    expected_ean = _fold(subject.ean)
    expected_brand = _fold(subject.brand)
    expected_model = _fold(subject.model)
    if not any((expected_reference, expected_ean, expected_brand, expected_model)):
        return None

    scored: list[tuple[int, PresentedCommerceProduct]] = []
    for product in products:
        if expected_reference and _fold(product.reference) == expected_reference:
            return CommerceProductReference.model_validate(product.model_dump(exclude={"position"}))
        if expected_ean and _fold(product.ean) == expected_ean:
            return CommerceProductReference.model_validate(product.model_dump(exclude={"position"}))
        text = _fold(" ".join(filter(None, (product.name, product.reference, product.brand))))
        score = 0
        if expected_brand:
            if expected_brand not in text:
                continue
            score += 2
        if expected_model:
            tokens = [token for token in expected_model.split() if token]
            matched = sum(1 for token in tokens if token in text)
            if tokens and matched != len(tokens):
                continue
            score += matched * 3
        if score:
            scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], item[1].position))
    if not scored:
        return None
    winner = scored[0][1]
    return CommerceProductReference.model_validate(winner.model_dump(exclude={"position"}))


def resolve_commerce_reference(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> tuple[CommerceProductReference | None, str]:
    reference_type = interpretation.reference_type
    if reference_type == "list_position" and interpretation.reference_position is not None:
        match = next(
            (
                product
                for product in state.last_presented_products
                if product.position == interpretation.reference_position
            ),
            None,
        )
        if match:
            return (
                CommerceProductReference.model_validate(match.model_dump(exclude={"position"})),
                "product_id",
            )
        return None, "none"
    if reference_type == "last_presented_product" and state.last_presented_products:
        match = state.last_presented_products[-1]
        return CommerceProductReference.model_validate(match.model_dump(exclude={"position"})), "product_id"
    if reference_type == "previous_recommendation" and state.last_presented_products:
        match = state.last_presented_products[0]
        return CommerceProductReference.model_validate(match.model_dump(exclude={"position"})), "product_id"
    if reference_type == "explicit_product":
        match = _explicit_product_match(interpretation, state.last_presented_products)
        return (match, "product_id" if match else "none")
    if reference_type == "current_product" and state.active_product:
        return state.active_product, "product_id"
    return None, "none"


def resolve_purchase_item_reference(
    item: PurchaseItem,
    state: CommerceConversationState,
) -> tuple[CommerceProductReference | None, str]:
    reference_type = item.reference_type
    if reference_type == "list_position" and item.reference_position is not None:
        match = next(
            (
                product
                for product in state.last_presented_products
                if product.position == item.reference_position
            ),
            None,
        )
        if match:
            return (
                CommerceProductReference.model_validate(
                    match.model_dump(exclude={"position"})
                ),
                "list_position",
            )
        return None, "none"
    if reference_type == "current_product" and state.active_product:
        return state.active_product, "active_product"
    if reference_type == "previous_recommendation" and state.last_presented_products:
        match = state.last_presented_products[0]
        return (
            CommerceProductReference.model_validate(
                match.model_dump(exclude={"position"})
            ),
            "previous_recommendation",
        )
    if reference_type == "last_presented_product" and state.last_presented_products:
        match = state.last_presented_products[-1]
        return (
            CommerceProductReference.model_validate(
                match.model_dump(exclude={"position"})
            ),
            "last_presented_product",
        )
    if reference_type == "explicit_product" and item.explicit_product_name:
        expected_tokens = [
            token
            for token in _fold(item.explicit_product_name).split()
            if token
        ]
        matches = []
        for product in state.last_presented_products:
            text = _fold(
                " ".join(
                    filter(None, (product.name, product.reference, product.brand))
                )
            )
            if expected_tokens and all(token in text for token in expected_tokens):
                matches.append(product)
        if len(matches) == 1:
            return (
                CommerceProductReference.model_validate(
                    matches[0].model_dump(exclude={"position"})
                ),
                "explicit_product",
            )
        return None, "ambiguous" if len(matches) > 1 else "none"
    return None, "none"


def apply_commerce_domain_context(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> tuple[SalesInterpretation, bool]:
    if interpretation._source == "openai":
        return interpretation, False
    previous_domain = state.active_domain
    if (
        previous_domain == "commerce"
        and interpretation.domain != "commerce"
        and interpretation.domain != "greeting"
        and not (
            interpretation._source != "openai"
            and interpretation.domain == "raffle"
        )
        and not interpretation.domain_change_explicit
    ):
        return interpretation.model_copy(update={"domain": "commerce"}), True
    return interpretation, False


def _compact_preferences(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def evolve_commerce_state(
    previous: CommerceConversationState,
    result: AgentResult,
) -> CommerceConversationState:
    state = previous.model_copy(deep=True)
    metadata = result.response_metadata or {}
    domain = metadata.get("domain")
    if domain in {"commerce", "raffle"}:
        state.active_domain = domain
    if domain != "commerce":
        return state

    if metadata.get("active_topic"):
        state.active_topic = str(metadata["active_topic"])
    if metadata.get("purchase_stage"):
        state.purchase_stage = str(metadata["purchase_stage"])
    if metadata.get("clear_pending_action"):
        state.pending_action = None
        state.pending_action_product_ids = []
    pending_action = metadata.get("pending_action")
    if pending_action in {
        "send_product_link",
        "create_cart",
        "show_images",
        "show_payment_options",
        "confirm_purchase",
    }:
        state.pending_action = pending_action
        pending_ids = metadata.get("pending_action_product_ids")
        state.pending_action_product_ids = [
            str(item)
            for item in pending_ids
            if item is not None
        ] if isinstance(pending_ids, list) else []
    cart_state = metadata.get("cart_state")
    if isinstance(cart_state, dict):
        for field in (
            "cart_id",
            "cart_session_id",
            "cart_url",
            "cart_product_id",
            "cart_variant_id",
            "cart_quantity",
            "cart_items",
        ):
            if field in cart_state:
                if field == "cart_items" and isinstance(cart_state[field], list):
                    parsed_items: list[CommerceCartItem] = []
                    for item in cart_state[field]:
                        try:
                            parsed_items.append(CommerceCartItem.model_validate(item))
                        except (TypeError, ValueError):
                            continue
                    state.cart_items = parsed_items
                else:
                    setattr(state, field, cart_state[field])
    active_preferences = _compact_preferences(metadata.get("active_preferences"))
    if active_preferences:
        state.active_preferences = active_preferences

    if metadata.get("clear_active_product"):
        state.active_product = None
    resolved = metadata.get("active_product")
    if isinstance(resolved, dict):
        try:
            state.active_product = CommerceProductReference.model_validate(resolved)
        except (TypeError, ValueError):
            pass

    products = (result.commercial_data or {}).get("products")
    compact_products: list[PresentedCommerceProduct] = []
    if isinstance(products, list):
        for position, product in enumerate(products[:3], start=1):
            if not isinstance(product, dict):
                continue
            identity = product_reference_from_product(product)
            if identity:
                compact_products.append(
                    PresentedCommerceProduct(position=position, **identity.model_dump())
                )
    if metadata.get("presented_products") and compact_products:
        state.last_presented_products = compact_products
    if metadata.get("activate_first_product") and compact_products:
        state.active_product = CommerceProductReference.model_validate(
            compact_products[0].model_dump(exclude={"position"})
        )
    return state
