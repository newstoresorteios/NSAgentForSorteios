from app.commerce.commerce_context import CommerceConversationState
from app.sales.responder import _responder_contract_for_turn
from app.sales_agent import (
    BASE_SALES_RESPONDER_INSTRUCTIONS,
    CHECKOUT_FLOW_INSTRUCTIONS,
    SALES_RESPONDER_INSTRUCTIONS,
)


def test_catalog_turn_omits_checkout_only_contract():
    contract = _responder_contract_for_turn(
        {"goal": "recommendation", "action": "recommendation"},
        CommerceConversationState(dialogue_phase="shortlist"),
    )

    assert contract == BASE_SALES_RESPONDER_INSTRUCTIONS
    assert CHECKOUT_FLOW_INSTRUCTIONS not in contract
    assert len(contract) < len(SALES_RESPONDER_INSTRUCTIONS)


def test_purchase_turn_keeps_checkout_contract():
    contract = _responder_contract_for_turn(
        {"goal": "buy", "action": "purchase_intent"},
        CommerceConversationState(dialogue_phase="buy"),
    )

    assert contract == SALES_RESPONDER_INSTRUCTIONS
    assert CHECKOUT_FLOW_INSTRUCTIONS in contract
