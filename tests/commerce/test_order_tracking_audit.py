import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.ops.handoff_service import is_handoff_acceptance, last_assistant_offered_handoff
from app.commerce.order_service import (
    _order_facts_result,
    is_order_notes_request,
    order_notes_unavailable_result,
)
from app.commerce.order_tracking_audit import audit_order_tracking, run_order_tracking_audit_batch


def test_is_order_notes_request_without_rastreio():
    state = CommerceConversationState(order_id="25522")
    assert is_order_notes_request(
        "tem alguma observação no pedido?",
        commerce_state=state,
    ) is True
    assert is_order_notes_request(
        "qual o código de rastreio?",
        commerce_state=state,
    ) is False


def test_order_notes_unavailable_offers_handoff():
    state = CommerceConversationState(order_id="25522")
    result = order_notes_unavailable_result(state)
    assert result.safety_reason == "order_notes_unavailable"
    assert "encaminhe" in result.reply_text.lower()
    assert result.response_metadata["handoff"]["offer"] is True
    assert result.response_metadata["pending_action"] == "awaiting_handoff_confirmation"


def test_shipped_without_tracking_offers_handoff():
    state = CommerceConversationState()
    result = _order_facts_result(
        {
            "order_id": "25522",
            "status": "ENVIADO",
            "status_group": "shipped",
        },
        "25522",
        state,
    )
    assert "encaminhe" in result.reply_text.lower()
    assert result.response_metadata["handoff"]["offer"] is True


def test_handoff_acceptance_after_tracking_offer():
    turns = [
        {
            "role": "assistant",
            "content": (
                "O pedido já foi enviado, mas o código de rastreio ainda não está "
                "cadastrado. Quer que eu encaminhe para a equipe confirmar?"
            ),
        }
    ]
    assert last_assistant_offered_handoff(turns) is True
    assert is_handoff_acceptance("sim", turns) is True


@pytest.mark.asyncio
async def test_audit_order_tracking_merges_shipping():
    async def execute(name, args):
        assert name == "get_order_complete"
        return {
            "order_id": args["order_id"],
            "status": "ENVIADO",
            "status_group": "shipped",
            "shipping": {"sending_code": "BR999"},
        }

    report = await audit_order_tracking("25522", execute=execute)
    assert report["ok"] is True
    assert report["tracking_present"] is True
    assert report["missing_tracking_when_shipped"] is False


@pytest.mark.asyncio
async def test_audit_batch_flags_missing_tracking():
    async def execute(name, args):
        return {
            "order_id": args["order_id"],
            "status": "ENVIADO",
            "status_group": "shipped",
        }

    batch = await run_order_tracking_audit_batch(
        execute=execute,
        sample_order_ids=["25522"],
        db_days=1,
    )
    assert batch["sample_count"] == 1
    assert batch["missing_tracking_when_shipped"]
    assert batch["alert"] is True
