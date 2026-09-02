"""Authoritative commerce dialogue phase (discovery → shortlist → buy → checkout)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from ..commerce_context import CommerceConversationState
from ..models import AgentResult, SalesInterpretation

DialoguePhase = Literal["discovery", "shortlist", "buy", "checkout"]

_CHECKOUT_PURCHASE_STAGES = frozenset({
    "cart_created",
    "checkout_channel_selection",
    "shipping",
    "checkout_ready",
    "checkout_data",
    "payment_discussion",
    "order_review",
    "order_created",
    "awaiting_payment",
    "payment_confirmed",
})

_CHECKOUT_PENDING_ACTIONS = frozenset({
    "show_payment_options",
    "confirm_purchase",
    "choose_checkout_channel",
    "awaiting_shipping_zipcode",
    "awaiting_shipping_selection",
    "awaiting_checkout_data",
    "awaiting_order_confirmation",
    "awaiting_payment",
    "awaiting_order_customer_document",
})

_BUY_PENDING_ACTIONS = frozenset({
    "create_cart",
    "send_product_link",
    "show_images",
    "show_nearby_line",
})

_NEW_BROWSE_RE = re.compile(
    r"\b("
    r"outras?\s+op(?:ç|c)(?:õ|o)es|outra\s+marca|outras?\s+marcas|"
    r"me\s+mostra|mostrar|ver\s+op(?:ç|c)(?:õ|o)es|sugest|"
    r"procuro|busco|quero\s+ver|quero\s+um\s+(?!d[eo]s?\b)|"
    r"gostaria\s+de\s+(?:ver|um|uma)"
    r")\b",
    re.IGNORECASE,
)

_SHORTLIST_REJECTION_RE = re.compile(
    r"^\s*(nenhuma|nenhum|nenhuma dessas|nenhum desses|"
    r"nao gostei de nenhuma|não gostei de nenhuma|"
    r"nao gostei de nenhum|não gostei de nenhum)\s*[!.?]*\s*$",
    re.IGNORECASE,
)


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def commerce_dialogue_phase(
    state: CommerceConversationState | None,
) -> DialoguePhase | None:
    if state is None:
        return None
    return state.dialogue_phase


def session_in_checkout_phase(
    state: CommerceConversationState | None,
) -> bool:
    """True when the customer is past shortlist selection into checkout."""
    if state is None:
        return False
    if state.cart_session_id:
        return True
    pending = state.pending_action
    if pending in _CHECKOUT_PENDING_ACTIONS:
        return True
    purchase_stage = str(state.purchase_stage or "")
    return purchase_stage in _CHECKOUT_PURCHASE_STAGES


def dialogue_phase_blocks_qualification(phase: DialoguePhase | None) -> bool:
    return phase in {"shortlist", "buy", "checkout"}


def is_open_sale_state(state: CommerceConversationState | None) -> bool:
    """Phone-level continuity: a live shortlist, lock, cart, or checkout."""
    if state is None:
        return False
    if state.dialogue_phase in {"shortlist", "buy", "checkout"}:
        return True
    if state.last_presented_products:
        return True
    prefs = state.active_preferences or {}
    if isinstance(prefs, dict) and prefs.get("locked_identity"):
        return True
    if state.cart_session_id or state.order_id:
        return True
    return False


def blocks_greeting_fast_path(state: CommerceConversationState | None) -> bool:
    """Skip Crono intro while a shortlist/lock is live. Checkout small-talk may greet."""
    if state is None:
        return False
    if session_in_checkout_phase(state) or state.dialogue_phase == "checkout":
        return False
    if state.dialogue_phase in {"shortlist", "buy"}:
        return True
    if state.last_presented_products:
        return True
    prefs = state.active_preferences or {}
    return bool(isinstance(prefs, dict) and prefs.get("locked_identity"))


def message_resets_dialogue_to_discovery(
    message_text: str | None,
    interpretation: SalesInterpretation | None,
) -> bool:
    folded = _fold(message_text)
    if _SHORTLIST_REJECTION_RE.match(folded):
        return True
    if interpretation is None:
        return False
    if _NEW_BROWSE_RE.search(folded):
        return True
    try:
        from .discovery import is_open_catalog_browse_request
        from ..catalog_specs import message_requests_other_brands

        if is_open_catalog_browse_request(message_text, interpretation):
            return True
        if message_requests_other_brands(message_text):
            return True
    except Exception:
        pass
    return False


def apply_dialogue_phase_discovery_gates(
    discovery_state: dict[str, Any],
    commerce_state: CommerceConversationState | None,
    *,
    message_text: str | None = None,
    interpretation: SalesInterpretation | None = None,
) -> dict[str, Any]:
    """Prevent persona qualify / vague-query reset when phase is past discovery."""
    updated = dict(discovery_state)
    phase = commerce_dialogue_phase(commerce_state)
    updated["dialogue_phase"] = phase

    if message_resets_dialogue_to_discovery(message_text, interpretation):
        updated["dialogue_phase"] = "discovery"
        return updated

    if updated.get("order_context_blocks_clarification"):
        updated["persona_qualification_required"] = False
        updated["force_retrieval"] = False
        return updated

    if dialogue_phase_blocks_qualification(phase):
        updated["persona_qualification_required"] = False
        updated["force_retrieval"] = False

    return updated


def resolve_dialogue_phase(
    previous: CommerceConversationState,
    metadata: dict[str, Any],
    result: AgentResult,
) -> DialoguePhase | None:
    """Infer the next dialogue phase from turn metadata and result payload."""
    explicit = metadata.get("dialogue_phase")
    if explicit in {"discovery", "shortlist", "buy", "checkout"}:
        return explicit  # type: ignore[return-value]
    if metadata.get("dialogue_phase_reset"):
        return "discovery"

    purchase_stage = str(metadata.get("purchase_stage") or previous.purchase_stage or "")
    pending = metadata.get("pending_action") or previous.pending_action

    if (
        previous.cart_session_id
        or previous.order_id
        or purchase_stage in _CHECKOUT_PURCHASE_STAGES
        or pending in _CHECKOUT_PENDING_ACTIONS
    ):
        return "checkout"

    if (
        purchase_stage == "selection"
        or pending in _BUY_PENDING_ACTIONS
        or metadata.get("purchase_action") == "create_cart"
    ):
        if previous.last_presented_products or previous.dialogue_phase in {"buy", "shortlist"}:
            return "buy"

    products = (result.commercial_data or {}).get("products")
    if metadata.get("presented_products") and isinstance(products, list) and products:
        return "shortlist"

    return None


def metadata_dialogue_phase_hint(
    *,
    interpretation: SalesInterpretation | None = None,
    message_text: str | None = None,
    presented_products: bool = False,
) -> dict[str, Any]:
    """Optional explicit phase hints for evolve_commerce_state."""
    if message_resets_dialogue_to_discovery(message_text, interpretation):
        return {"dialogue_phase": "discovery", "dialogue_phase_reset": True}
    if presented_products:
        return {"dialogue_phase": "shortlist"}
    if interpretation is not None and (
        interpretation.purchase_action == "create_cart"
        or interpretation.goal == "buy"
    ):
        return {"dialogue_phase": "buy"}
    return {}
