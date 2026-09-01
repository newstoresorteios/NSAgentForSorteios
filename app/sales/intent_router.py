"""Minimal sales intent router — discovery, purchase close, qualification gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..commerce_context import CommerceConversationState
from ..models import SalesInterpretation
from .discovery import _discovery_state, _needs_clarification_before_retrieval

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
    )
