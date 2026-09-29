import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.commerce.context_boundaries import reset_for_new_request, ambiguous_number_question
from app.commerce.order_context_recovery import extract_handles_from_conversation
from app.memory.context_resume import merge_commerce_states


@pytest.mark.parametrize("text,cid", [
    ("Quero fazer um novo pedido", "same"),
    ("Quero outro orçamento", "same"),
    ("Vamos começar uma nova conversa", "same"),
    ("E o pedido 27222?", "same"),
    ("sim", "new"),
    ("2500", "new"),
])
def test_new_request_cannot_inherit_budget_order_or_payment(text, cid):
    old = CommerceConversationState(
        last_conversation_id="same", order_id="26116", order_lookup_id="26116",
        order_payment_url="https://example.com/old-payment",
        pending_action="awaiting_payment", active_preferences={"budget_max": 5000},
    )
    new, changed = reset_for_new_request(old, text, cid, 1030)
    assert changed
    assert new.order_id is None and new.order_lookup_id is None
    assert new.order_payment_url is None and not new.active_preferences
    assert new.history_cut_inbound_id == 1030
    merged = merge_commerce_states(new.model_dump(), old.model_dump())
    assert merged["order_id"] is None
    assert merged["order_payment_url"] is None
    assert old.order_id == "26116"


def test_same_thread_followup_preserves_clear_order():
    old = CommerceConversationState(last_conversation_id="same", order_id="26116")
    new, changed = reset_for_new_request(old, "Despachou amigão?", "same", 1030)
    assert not changed
    assert new.order_id == "26116"


@pytest.mark.parametrize("prompt,confirm", [
    ("Qual seu orçamento?", False),
    ("Qual a faixa de investimento?", False),
    ("Qual o número do pedido?", False),
    ("Qual o número do pedido ou seu orçamento?", True),
    ("Boa tarde!", True),
])
def test_number_requires_clear_immediate_context(prompt, confirm):
    turns = [{"role": "assistant", "content": prompt}]
    handles = extract_handles_from_conversation(
        state=CommerceConversationState(order_id="26116"),
        recent_turns=turns, message_text="26116",
    )
    assert bool(ambiguous_number_question("26116", turns, handles)) == confirm


def test_two_explicit_old_orders_require_confirmation():
    handles = extract_handles_from_conversation(
        state=CommerceConversationState(),
        recent_turns=[{"role": "user", "content": "pedido 26116"},
                      {"role": "user", "content": "pedido 27222"}],
        message_text="Despachou?",
    )
    assert handles["order_ids"] == []
    assert handles["ambiguous_order_ids"] == ["26116", "27222"]


@pytest.mark.asyncio
async def test_budget_equal_to_previous_order_does_not_trigger_order_lookup():
    from app.agents.door_order import try_order_resume_route
    from app.models import IncomingMessage
    result = await try_order_resume_route(
        message=IncomingMessage(text="26116"),
        commerce_state=CommerceConversationState(order_id="26116"),
        context_handles={"order_ids": ["26116"], "contextual_order_ids": []},
        fresh_start=False, soft_greeting=False,
        resume_pending_order_early=True, order_reference=None,
    )
    assert result is None
