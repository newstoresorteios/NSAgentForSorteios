from app.commerce.commerce_context import CommerceConversationState
from app.sales.dialogue_phase import (
    is_fresh_commerce_start,
    message_resets_dialogue_to_discovery,
    reset_browse_memory_keep_orders,
)


def test_fresh_start_phrases_are_generic_and_narrow():
    assert is_fresh_commerce_start("quero começar uma conversa nova")
    assert is_fresh_commerce_start("vamos começar de novo")
    assert is_fresh_commerce_start("outra conversa")
    assert is_fresh_commerce_start("recomeçar")
    assert is_fresh_commerce_start("do zero")
    assert is_fresh_commerce_start("oi") is False
    assert is_fresh_commerce_start("esquece") is False
    assert is_fresh_commerce_start("me mostra outras opções") is False
    assert is_fresh_commerce_start("quero ver seiko") is False


def test_fresh_start_resets_shortlist_and_keeps_unpaid_order():
    state = CommerceConversationState(
        active_domain="commerce",
        dialogue_phase="shortlist",
        last_presented_products=[
            {"position": 1, "product_id": "641", "name": "Tissot", "brand": "Tissot"},
        ],
        active_product={"product_id": "641", "name": "Tissot", "brand": "Tissot"},
        active_topic="tissot",
        active_preferences={"locked_identity": {"product_id": "641"}},
        order_id="25422",
        order_payment_url="https://pay.example/1",
        pending_action="awaiting_payment",
    )
    reset = reset_browse_memory_keep_orders(state)
    assert reset.last_presented_products == []
    assert reset.active_product is None
    assert reset.active_topic is None
    assert reset.dialogue_phase == "discovery"
    assert reset.forget_shortlist is True
    assert reset.order_id == "25422"
    assert reset.order_payment_url == "https://pay.example/1"
    assert reset.pending_action == "awaiting_payment"
    assert (reset.active_preferences or {}).get("locked_identity") is None
    assert message_resets_dialogue_to_discovery(
        "quero começar uma conversa nova",
        None,
    )
