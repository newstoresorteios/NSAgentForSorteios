"""Authoritative commerce dialogue phase (discovery → shortlist → buy → checkout)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.commerce.commerce_context import CommerceConversationState
from ..models import AgentResult, SalesInterpretation

# Overnight WhatsApp pause: next greeting/new thread drops the old shortlist.
BROWSE_IDLE_SECONDS = 12 * 60 * 60

_CATALOG_PREF_KEYS = (
    "locked_identity",
    "budget",
    "budget_max",
    "color",
    "occasion",
    "style",
    "excluded_product_ids",
)

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

_FRESH_START_RE = re.compile(
    r"\b("
    r"come[cç]ar\s+(de\s+novo|outra|do\s+zero|uma\s+nova|uma\s+conversa)|"
    r"recome[cç]ar|"
    r"nova\s+conversa|outra\s+conversa|conversa\s+nova|"
    r"do\s+zero|"
    r"vamos\s+come[cç]ar\s+outra"
    r")\b",
    re.IGNORECASE,
)


def is_fresh_commerce_start(message_text: str | None) -> bool:
    """Customer asked to start over — not a follow-up on the last shortlist."""
    return bool(_FRESH_START_RE.search(_fold(message_text)))


def _scrub_catalog_preferences(prefs: dict[str, Any] | None) -> dict[str, Any]:
    cleaned = dict(prefs or {})
    for key in _CATALOG_PREF_KEYS:
        cleaned.pop(key, None)
    return cleaned


def has_browse_memory(state: CommerceConversationState | None) -> bool:
    """True when a prior shortlist/lock would leak into the next answer."""
    if state is None:
        return False
    if state.last_presented_products or state.active_product is not None:
        return True
    if state.active_topic:
        return True
    prefs = state.active_preferences or {}
    return bool(isinstance(prefs, dict) and prefs.get("locked_identity"))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_browse_idle(
    state: CommerceConversationState | None,
    *,
    now: datetime | None = None,
    idle_seconds: int = BROWSE_IDLE_SECONDS,
) -> bool:
    if state is None or not has_browse_memory(state):
        return False
    stamped = _as_utc(state.last_browse_at)
    if stamped is None:
        return False
    current = _as_utc(now) or datetime.now(timezone.utc)
    return current - stamped >= timedelta(seconds=idle_seconds)


def is_new_commerce_thread(
    conversation_id: str | None,
    state: CommerceConversationState | None,
) -> bool:
    incoming = str(conversation_id or "").strip()
    stored = str(getattr(state, "last_conversation_id", None) or "").strip()
    if not incoming or not stored:
        return False
    return incoming != stored


def is_commerce_continuation(message_text: str | None) -> bool:
    """Follow-up on the live sale — do not drop the shortlist for a new thread/idle."""
    try:
        from .purchase_selection import (
            is_bare_purchase_closing,
            is_checkout_utterance,
            parse_list_position_selection,
        )
        from app.memory.context_resume import (
            is_payment_link_request,
            is_presented_catalog_question,
            is_short_affirmation,
            is_unpaid_order_resume_request,
        )

        if parse_list_position_selection(message_text):
            return True
        if is_checkout_utterance(message_text) or is_bare_purchase_closing(message_text):
            return True
        if is_payment_link_request(message_text) or is_unpaid_order_resume_request(
            message_text
        ):
            return True
        if is_short_affirmation(message_text):
            return True
        if is_presented_catalog_question(message_text):
            return True
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("dialogue_phase.continuation_detect", exc)
    return False


def is_session_opener_greeting(message_text: str | None) -> bool:
    """Bare hello — WhatsApp often keeps the same conversation_id for a new chat."""
    try:
        from app.identity.greeting_policy import is_any_greeting

        return is_any_greeting(message_text)
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("dialogue_phase.session_opener", exc)
        return False


def should_reset_browse_memory(
    message_text: str | None,
    *,
    conversation_id: str | None = None,
    state: CommerceConversationState | None = None,
    now: datetime | None = None,
) -> bool:
    """New session opener — do not volunteer the previous shortlist."""
    if is_fresh_commerce_start(message_text):
        return True
    if not has_browse_memory(state):
        return False
    if is_commerce_continuation(message_text):
        return False
    if is_session_opener_greeting(message_text):
        return True
    if getattr(state, "closed_by_farewell", False):
        return True
    if is_new_commerce_thread(conversation_id, state):
        return True
    if is_browse_idle(state, now=now):
        return True
    return False


def reset_browse_memory_keep_orders(
    state: CommerceConversationState,
) -> CommerceConversationState:
    """Drop catalog memory; keep unpaid order / cart so payment resume still works."""
    updated = state.model_copy(deep=True)
    updated.last_presented_products = []
    updated.active_product = None
    updated.active_topic = None
    updated.dialogue_phase = "discovery"
    updated.forget_shortlist = True
    updated.product_resolution_state = None
    updated.closed_by_farewell = False
    updated.last_browse_at = None
    updated.active_preferences = _scrub_catalog_preferences(updated.active_preferences)
    return updated


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
    if is_fresh_commerce_start(message_text):
        return True
    if _SHORTLIST_REJECTION_RE.match(folded):
        return True
    if interpretation is None:
        return False
    if _NEW_BROWSE_RE.search(folded):
        return True
    try:
        from .discovery import is_open_catalog_browse_request
        from app.catalog.specs.catalog_specs import message_requests_other_brands

        if is_open_catalog_browse_request(message_text, interpretation):
            return True
        if message_requests_other_brands(message_text):
            return True
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("dialogue_phase.browse_detect", exc)
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
