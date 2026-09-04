from datetime import datetime, timedelta, timezone

from app.commerce.commerce_context import CommerceConversationState
from app.models import SalesInterpretation
from app.sales.dialogue_phase import (
    BROWSE_IDLE_SECONDS,
    is_fresh_commerce_start,
    message_resets_dialogue_to_discovery,
    reset_browse_memory_keep_orders,
    should_reset_browse_memory,
)
from app.sales.purchase_selection import is_checkout_utterance
from app.sales.turn_contract import next_locked_identity


def _shortlist_state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "dialogue_phase": "shortlist",
        "last_presented_products": [
            {"position": 1, "product_id": "641", "name": "Tissot PRX", "brand": "Tissot"},
        ],
        "active_product": {"product_id": "641", "name": "Tissot PRX", "brand": "Tissot"},
        "active_topic": "tissot",
        "active_preferences": {
            "locked_identity": {"brand": "Tissot", "model": "PRX"},
            "budget_max": 5000,
            "color": "azul",
        },
        "order_id": "25422",
        "order_payment_url": "https://pay.example/1",
        "pending_action": "awaiting_payment",
        "last_conversation_id": "thread-old",
        "last_browse_at": datetime.now(timezone.utc),
        "closed_by_farewell": False,
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interp(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {"product_type": "relógio", "brand": "Tissot", "model": "PRX"},
        "references_previous_context": False,
        "needs_clarification": False,
        "enough_information_to_search": True,
        "ready_for_retrieval": True,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


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
    state = _shortlist_state()
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
    assert (reset.active_preferences or {}).get("budget_max") is None
    assert (reset.active_preferences or {}).get("color") is None
    assert message_resets_dialogue_to_discovery(
        "quero começar uma conversa nova",
        None,
    )


def test_new_conversation_id_resets_shortlist_keeps_order():
    state = _shortlist_state()
    assert should_reset_browse_memory(
        "oi",
        conversation_id="thread-new",
        state=state,
    )
    reset = reset_browse_memory_keep_orders(state)
    assert reset.last_presented_products == []
    assert reset.order_id == "25422"


def test_new_conversation_id_resets_on_new_catalog_ask():
    state = _shortlist_state()
    assert should_reset_browse_memory(
        "quero um seiko",
        conversation_id="thread-new",
        state=state,
    )


def test_greeting_opens_new_session_without_volunteering_shortlist():
    """Same WhatsApp id, bare hello — do not resend the previous list."""
    state = _shortlist_state()
    assert should_reset_browse_memory(
        "oi",
        conversation_id="thread-old",
        state=state,
    )
    assert should_reset_browse_memory(
        "boa noite",
        conversation_id="thread-old",
        state=state,
    )
    assert should_reset_browse_memory(
        "me mostra de novo",
        conversation_id="thread-old",
        state=state,
    ) is False
    assert should_reset_browse_memory(
        "quais eram os relogios",
        conversation_id="thread-old",
        state=state,
    ) is False


def test_new_conversation_id_keeps_shortlist_on_position_pick():
    state = _shortlist_state()
    assert should_reset_browse_memory(
        "quero o 2",
        conversation_id="thread-new",
        state=state,
    ) is False


def test_farewell_then_greeting_resets_shortlist():
    state = _shortlist_state(closed_by_farewell=True)
    assert should_reset_browse_memory(
        "oi",
        conversation_id="thread-old",
        state=state,
    )
    assert should_reset_browse_memory(
        "quero o 2",
        conversation_id="thread-old",
        state=state,
    ) is False


def test_idle_greeting_resets_shortlist():
    old = datetime.now(timezone.utc) - timedelta(seconds=BROWSE_IDLE_SECONDS + 60)
    state = _shortlist_state(last_browse_at=old)
    assert should_reset_browse_memory(
        "oi",
        conversation_id="thread-old",
        state=state,
    )


def test_idle_position_pick_keeps_shortlist():
    old = datetime.now(timezone.utc) - timedelta(seconds=BROWSE_IDLE_SECONDS + 60)
    state = _shortlist_state(last_browse_at=old)
    assert should_reset_browse_memory(
        "o 2",
        conversation_id="thread-old",
        state=state,
    ) is False


def test_same_thread_new_catalog_ask_resets_shortlist():
    state = _shortlist_state()
    assert should_reset_browse_memory(
        "quero relógios até 2500",
        conversation_id="thread-old",
        state=state,
    )
    assert should_reset_browse_memory(
        "qualquer marca",
        conversation_id="thread-old",
        state=state,
    )
    assert should_reset_browse_memory(
        "quero um seiko",
        conversation_id="thread-old",
        state=state,
    )


def test_filling_in_conversation_id_is_not_a_new_thread():
    state = _shortlist_state(last_conversation_id=None)
    assert should_reset_browse_memory(
        "tem em preto?",
        conversation_id="thread-first",
        state=state,
    ) is False


def test_soft_model_browse_does_not_lock_identity():
    state = _shortlist_state(active_preferences={})
    locked = next_locked_identity(
        _interp(goal="discover", subject={"brand": "Tissot", "model": "PRX"}),
        state,
        "tem tissot prx?",
    )
    assert locked is None


def test_explicit_list_pick_locks_presented_identity():
    state = _shortlist_state(active_preferences={})
    locked = next_locked_identity(
        _interp(goal="buy", purchase_action="create_cart"),
        state,
        "quero o 1",
    )
    assert locked is not None
    assert locked.get("brand") == "Tissot"


def test_existing_lock_survives_followup_without_new_pick():
    state = _shortlist_state()
    locked = next_locked_identity(
        _interp(goal="inspect", subject={"brand": "Tissot"}),
        state,
        "tem em preto?",
    )
    assert locked == {"brand": "Tissot", "model": "PRX"}


def test_checkout_utterance_is_not_generic_buy():
    assert is_checkout_utterance("pode montar o pedido pra mim")
    assert is_checkout_utterance("quero fechar")
    assert is_checkout_utterance("me manda o link de pagamento")
    assert is_checkout_utterance("como podes fazer pra fechar a compra?")
    assert is_checkout_utterance("quero comprar um relógio") is False
    assert is_checkout_utterance("procuro um seiko") is False
