"""Unit tests for sales intent router."""

from __future__ import annotations

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import SalesInterpretation
from app.sales.intent_router import route_sales_intent


def _interp(**kwargs) -> SalesInterpretation:
    base = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {},
        "preferences": {},
        "references_previous_context": False,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "needs_clarification": True,
        "confidence": 0.9,
    }
    base.update(kwargs)
    return SalesInterpretation(**base)


def _plan(**kwargs) -> dict:
    base = {
        "intent": "clarification",
        "goal": "discover",
        "query": "alguma coisa",
    }
    base.update(kwargs)
    return base


@pytest.mark.offline_eval
def test_force_retrieval_promotes_clarification_to_recommendation():
    interpretation = _interp(
        goal="recommend",
        subject={"product_type": "relógio", "brand": "Seiko"},
        preferences={"budget_max": 5000, "style": "esportivo"},
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
    )
    route = route_sales_intent(
        interpretation=interpretation,
        plan=_plan(intent="clarification"),
        message_text="quero ver opções",
        commerce_state=CommerceConversationState(),
        recent_turns=[],
    )
    assert route.force_retrieval is True
    assert route.plan_intent == "recommendation"


@pytest.mark.offline_eval
def test_purchase_close_skips_qualification_and_force_retrieval():
    presented = [
        {
            "position": 1,
            "product_id": "aq1",
            "name": "Baltic Aquascaphe",
            "brand": "Baltic",
        },
        {
            "position": 2,
            "product_id": "sb01",
            "name": "Baltic Classic SB01",
            "brand": "Baltic",
        },
    ]
    interpretation = _interp(
        goal="buy",
        purchase_action="create_cart",
        stop_clarification=True,
        needs_clarification=False,
    )
    state = CommerceConversationState(last_presented_products=presented)
    route = route_sales_intent(
        interpretation=interpretation,
        plan=_plan(intent="clarification", query=""),
        message_text="quero comprar o 2",
        commerce_state=state,
        recent_turns=[],
    )
    assert route.purchase_close is True
    assert route.skip_qualification is True
    assert route.discovery_state is not None
    assert route.discovery_state["persona_qualification_required"] is False
    assert route.discovery_state["force_retrieval"] is False
    assert route.purchase_close_hold is True


@pytest.mark.offline_eval
def test_vague_query_triggers_clarification_when_not_purchase_close():
    interpretation = _interp(
        goal="discover",
        subject={"product_type": "relógio"},
        needs_clarification=True,
    )
    route = route_sales_intent(
        interpretation=interpretation,
        plan=_plan(intent="clarification", query="alguma coisa"),
        message_text="quero um relógio",
        commerce_state=CommerceConversationState(),
        recent_turns=[],
    )
    assert route.purchase_close is False
    assert route.vague_query is True
    assert route.vague_query_clarification is True
    assert route.purchase_close_hold is False


@pytest.mark.offline_eval
def test_needs_clarification_before_retrieval_for_discover_goal():
    interpretation = _interp(
        goal="discover",
        subject={"product_type": "relógio"},
        needs_clarification=True,
    )
    route = route_sales_intent(
        interpretation=interpretation,
        plan=_plan(intent="recommendation", query="relógio"),
        message_text="quero um relógio",
        commerce_state=CommerceConversationState(),
        recent_turns=[],
    )
    assert route.needs_clarification_before_retrieval is True
    assert route.force_retrieval is False
