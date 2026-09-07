"""Minimal sales intent router — discovery, purchase close, qualification gates."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from app.commerce.commerce_context import CommerceConversationState
from ..models import SalesInterpretation
from .discovery import _discovery_state, _needs_clarification_before_retrieval

_GENERIC_LOOKUP_MODELS = frozenset({"relogio", "watch", "produto", "product"})


def _fold_lookup_model(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch)).strip()


def has_compiled_lookup_identity(interpretation: SalesInterpretation | None) -> bool:
    """True when close can look up a named SKU instead of holding the shortlist."""
    if interpretation is None:
        return False
    subject = interpretation.subject
    if subject.reference or subject.ean:
        return True
    model = str(subject.model or "").strip()
    if not model:
        return False
    return _fold_lookup_model(model) not in _GENERIC_LOOKUP_MODELS

RouteKind = Literal[
    "browse", "refine", "reject", "close", "inspect", "talk", "qualify"
]

_VAGUE_QUERIES = frozenset(
    {
        "",
        "alguma coisa",
        "algo",
        "qualquer coisa",
        "um produto",
        "uma coisa",
        "produto",
    }
)


@dataclass(frozen=True)
class SalesIntentRoute:
    """Consolidated routing flags for commerce turns."""

    discovery_state: dict[str, Any] | None = None
    plan_intent: str | None = None
    browse_reset: bool = False
    skip_qualification: bool = False
    purchase_close: bool = False
    force_retrieval: bool = False
    needs_clarification_before_retrieval: bool = False
    vague_query: bool = False
    vague_query_clarification: bool = False
    purchase_close_hold: bool = False
    skip_catalog_fanout: bool = False
    route_kind: RouteKind = "browse"

    def blocks_compiled_product_retrieval(
        self,
        resolved_product: Any = None,
        interpretation: SalesInterpretation | None = None,
    ) -> bool:
        """Close without a bound or named SKU must not fan-out Tray list search."""
        if resolved_product is not None or self.route_kind != "close":
            return False
        return not has_compiled_lookup_identity(interpretation)


_TALK_STRATEGIES = frozenset(
    {"acknowledge", "clarify", "handoff", "refuse"}
)


def classify_sales_route_kind(
    *,
    interpretation: SalesInterpretation | None,
    message_text: str | None,
    commerce_state: CommerceConversationState | None,
    browse_reset: bool,
    purchase_close: bool,
    skip_catalog_fanout: bool,
    needs_clarification_before_retrieval: bool,
    vague_query_clarification: bool,
) -> RouteKind:
    """Policy table: buy/reject/refine/talk — not one regex per incident."""
    if browse_reset:
        try:
            from app.catalog.specs.catalog_specs import (
                extract_rejected_brands_from_text,
                message_requests_other_brands,
            )

            if message_requests_other_brands(message_text) or extract_rejected_brands_from_text(
                message_text
            ):
                return "reject"
        except Exception as exc:
            from app.sales import log_swallowed

            log_swallowed("intent_router.reject_brands", exc)
        return "browse"
    try:
        from .purchase_selection import (
            is_bare_purchase_closing,
            is_checkout_utterance,
            parse_list_position_selection,
        )

        if (
            parse_list_position_selection(message_text)
            or is_bare_purchase_closing(message_text)
            or is_checkout_utterance(message_text)
        ):
            return "close"
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("intent_router.close_utterance", exc)
    if interpretation is not None and interpretation.purchase_action in {
        "create_cart",
        "show_cart_link",
        "checkout_question",
    }:
        return "close"
    if interpretation is not None and interpretation.reference_position is not None:
        return "close"
    if skip_catalog_fanout:
        if interpretation is not None and interpretation.goal == "inspect":
            return "inspect"
        return "talk"
    if needs_clarification_before_retrieval or vague_query_clarification:
        return "qualify"
    if commerce_state is not None and (
        commerce_state.active_product is not None
        or commerce_state.last_presented_products
    ):
        prefs = getattr(interpretation, "preferences", None) if interpretation else None
        if prefs is not None and (prefs.color or prefs.material):
            return "refine"
    return "browse"


def should_skip_catalog_fanout(interpretation: SalesInterpretation | None) -> bool:
    """Talk/inspect turns must not fan-out Tray list search."""
    if interpretation is None:
        return False
    if getattr(interpretation, "_slot_answer_hold", False):
        return True
    if interpretation.purchase_action:
        return False
    strategy = interpretation.resolved_answer_strategy()
    if strategy in _TALK_STRATEGIES:
        return True
    if strategy == "answer_directly" and interpretation.goal == "inspect":
        return True
    return False


def route_sales_intent(
    *,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    message_text: str | None,
    commerce_state: CommerceConversationState | None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> SalesIntentRoute:
    """Delegate to discovery / purchase_selection / qualification slot helpers."""
    plan_intent = str(plan.get("intent") or "") or None
    if interpretation is None:
        return SalesIntentRoute(plan_intent=plan_intent)

    discovery_state = _discovery_state(
        interpretation,
        recent_turns,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    force_retrieval = bool(discovery_state.get("force_retrieval"))
    if force_retrieval and plan_intent == "clarification":
        plan_intent = "recommendation"

    browse_reset = False
    try:
        from .dialogue_phase import message_resets_dialogue_to_discovery

        browse_reset = message_resets_dialogue_to_discovery(
            message_text,
            interpretation,
        )
    except Exception:
        browse_reset = False

    from .purchase_selection import blocks_persona_qualification_for_purchase

    purchase_close = (
        False
        if browse_reset
        else blocks_persona_qualification_for_purchase(
            interpretation,
            commerce_state,
        )
    )
    skip_qualification = purchase_close
    if discovery_state and not browse_reset and skip_qualification:
        discovery_state = {
            **discovery_state,
            "persona_qualification_required": False,
            "force_retrieval": False,
        }
        force_retrieval = False

    vague_query = str(plan.get("query") or "").strip().lower() in _VAGUE_QUERIES
    routed_plan = {**plan, "intent": plan_intent or plan.get("intent")}
    needs_clarification_before_retrieval = False
    if discovery_state:
        needs_clarification_before_retrieval = _needs_clarification_before_retrieval(
            interpretation,
            routed_plan,
            discovery_state,
        )
        force_retrieval = bool(discovery_state.get("force_retrieval"))

    vague_query_clarification = (
        vague_query and not force_retrieval and not skip_qualification
    )
    from .dialogue_phase import session_in_checkout_phase

    purchase_close_hold = (
        skip_qualification
        and not browse_reset
        and not session_in_checkout_phase(commerce_state)
        and (plan_intent == "clarification" or vague_query)
    )
    skip_catalog_fanout = should_skip_catalog_fanout(interpretation)
    if discovery_state and discovery_state.get("slot_answer_hold"):
        skip_catalog_fanout = True
    route_kind = classify_sales_route_kind(
        interpretation=interpretation,
        message_text=message_text,
        commerce_state=commerce_state,
        browse_reset=browse_reset,
        purchase_close=purchase_close,
        skip_catalog_fanout=skip_catalog_fanout,
        needs_clarification_before_retrieval=needs_clarification_before_retrieval,
        vague_query_clarification=vague_query_clarification,
    )

    return SalesIntentRoute(
        discovery_state=discovery_state,
        plan_intent=plan_intent,
        browse_reset=browse_reset,
        skip_qualification=skip_qualification,
        purchase_close=purchase_close,
        force_retrieval=force_retrieval,
        needs_clarification_before_retrieval=needs_clarification_before_retrieval,
        vague_query=vague_query,
        vague_query_clarification=vague_query_clarification,
        purchase_close_hold=purchase_close_hold,
        skip_catalog_fanout=skip_catalog_fanout,
        route_kind=route_kind,
    )
