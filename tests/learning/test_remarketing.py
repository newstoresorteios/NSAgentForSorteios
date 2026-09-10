from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import BrevoSendResult, IncomingMessage
from app.learning.remarketing import (
    _build_remarketing_message,
    _remarketing_stage,
    is_remarketing_opt_out,
    remarketing_identity_key,
)


def test_opt_out_requires_a_clear_standalone_command():
    assert is_remarketing_opt_out("SAIR") is True
    assert is_remarketing_opt_out("Não quero mais mensagens") is True
    assert is_remarketing_opt_out("Como faço para sair da conta?") is False


def test_remarketing_identity_keeps_the_omnichannel_sender_key():
    incoming = IncomingMessage(
        channel="instagram",
        sender_key="instagram:user-123",
        visitor_id="visitor-123",
        conversation_id="conversation-123",
    )

    assert remarketing_identity_key(incoming) == "instagram:user-123"


def test_stage_prioritizes_payment_checkout_and_cart():
    assert _remarketing_stage({"order_payment_status": "pending"}) == "awaiting_payment"
    assert _remarketing_stage({"purchase_stage": "shipping"}) == "checkout"
    assert _remarketing_stage({"cart_session_id": "cart-1"}) == "cart"
    assert _remarketing_stage({"active_product": {"name": "Relógio"}}) == "product_selection"


def test_message_is_stage_specific_and_always_contains_opt_out():
    text = _build_remarketing_message(
        {
            "sender_name": "Maria Silva",
            "stage": "cart",
            "touch_number": 3,
            "cart_url": "https://loja.example/carrinho",
        }
    )

    assert text.startswith("Oi!")
    assert "Maria" not in text
    assert "última mensagem" in text
    assert "https://loja.example/carrinho" in text
    assert "responda SAIR" in text


@pytest.mark.parametrize(
    "nickname",
    ["Corn", "Razor", "Dark", "Razor Blue", "Dark Orange", "Bed Linen", "Nebula", "Maria"],
)
def test_remarketing_never_addresses_customer_by_brevo_nickname(nickname):
    text = _build_remarketing_message(
        {
            "sender_name": nickname,
            "stage": "cart",
            "touch_number": 1,
        }
    )

    assert text.startswith("Oi!")
    assert nickname.split()[0] not in text


def test_customer_reply_closes_previous_inactivity_cycle_in_source():
    from pathlib import Path

    source = Path("app/learning/remarketing.py").read_text(encoding="utf-8")

    assert "completion_reason = 'customer_reengaged'" in source
    assert "active_id = None" in source
    assert "status IN ('pending', 'processing', 'failed')" in source


@pytest.mark.asyncio
async def test_batch_replies_only_through_the_origin_channel(monkeypatch):
    import app.learning.remarketing as remarketing

    sent = []
    finished = []
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 10,
                "conversation_status_id": 20,
                "touch_number": 1,
                "stage": "product_selection",
                "product_name": "Relógio",
                "channel": "instagram",
                "sender_key": "instagram:user-1",
                "visitor_id": "visitor-1",
                "conversation_id": "conversation-1",
                "sender_name": "Ana",
                "order_id": None,
                "cart_url": None,
                "payment_url": None,
            }
        ],
    )
    monkeypatch.setattr(remarketing, "remarketing_attempt_is_sendable", lambda *_args, **_kwargs: True)

    async def fake_send(incoming, text):
        sent.append((incoming, text))
        return BrevoSendResult(ok=True, dry_run=False)

    monkeypatch.setattr(remarketing, "send_brevo_reply", fake_send)
    monkeypatch.setattr(
        remarketing,
        "finish_remarketing_attempt",
        lambda attempt_id, **kwargs: finished.append((attempt_id, kwargs)),
    )

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 1, "failed": 0}
    assert sent[0][0].channel == "instagram"
    assert sent[0][0].visitor_id == "visitor-1"
    assert finished[0][0] == 10
    assert finished[0][1]["send_ok"] is True


@pytest.mark.asyncio
async def test_paid_order_is_closed_before_any_remarketing_send(monkeypatch):
    import app.learning.remarketing as remarketing

    completed = []
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 11,
                "conversation_status_id": 21,
                "touch_number": 1,
                "stage": "awaiting_payment",
                "channel": "whatsapp",
                "sender_phone": "5511999999999",
                "order_id": "order-1",
            }
        ],
    )
    monkeypatch.setattr(remarketing, "remarketing_attempt_is_sendable", lambda *_args, **_kwargs: True)

    class Client:
        async def get_order_payment(self, _order_id):
            return {"success": True, "payment": {"has_payment": True}}

    monkeypatch.setattr(remarketing, "TrayAdapterClient", Client)
    monkeypatch.setattr(
        remarketing,
        "complete_paid_remarketing",
        lambda conversation_id, attempt_id: completed.append(
            (conversation_id, attempt_id)
        ),
    )

    async def fail_send(*_args):
        raise AssertionError("paid customer must not receive remarketing")

    monkeypatch.setattr(remarketing, "send_brevo_reply", fail_send)

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 0, "failed": 0}
    assert completed == [(21, 11)]


@pytest.mark.asyncio
async def test_customer_reply_after_claim_suppresses_stale_remarketing(monkeypatch):
    import app.learning.remarketing as remarketing

    claimed_at = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 12,
                "conversation_status_id": 22,
                "touch_number": 1,
                "stage": "product_selection",
                "channel": "whatsapp",
                "sender_phone": "5511999999999",
                "claimed_at": claimed_at,
            }
        ],
    )
    checks = []

    def cancelled_after_claim(attempt_id, *, claimed_at):
        checks.append((attempt_id, claimed_at))
        return False

    monkeypatch.setattr(
        remarketing,
        "remarketing_attempt_is_sendable",
        cancelled_after_claim,
    )
    send = MagicMock(side_effect=AssertionError("cancelled touch must not be sent"))
    finish = MagicMock(side_effect=AssertionError("inbound cancellation owns final state"))
    monkeypatch.setattr(remarketing, "send_brevo_reply", send)
    monkeypatch.setattr(remarketing, "finish_remarketing_attempt", finish)

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 0, "failed": 0}
    assert checks == [(12, claimed_at)]
    send.assert_not_called()
    finish.assert_not_called()


@pytest.mark.asyncio
async def test_customer_reply_during_tray_check_suppresses_remarketing(monkeypatch):
    import app.learning.remarketing as remarketing

    claimed_at = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(remarketing_enabled=True, remarketing_batch_size=25),
    )
    monkeypatch.setattr(
        remarketing,
        "claim_due_remarketing_attempts",
        lambda _limit: [
            {
                "id": 13,
                "conversation_status_id": 23,
                "touch_number": 1,
                "stage": "awaiting_payment",
                "channel": "whatsapp",
                "sender_phone": "5511999999999",
                "order_id": "order-13",
                "claimed_at": claimed_at,
            }
        ],
    )
    sendability = iter((True, False))
    monkeypatch.setattr(
        remarketing,
        "remarketing_attempt_is_sendable",
        lambda *_args, **_kwargs: next(sendability),
    )

    class Client:
        async def get_order_payment(self, _order_id):
            return {"success": True, "payment": {"has_payment": False}}

    monkeypatch.setattr(remarketing, "TrayAdapterClient", Client)
    send = MagicMock(side_effect=AssertionError("stale touch must not be sent"))
    finish = MagicMock(side_effect=AssertionError("cancelled attempt must not be rewritten"))
    monkeypatch.setattr(remarketing, "send_brevo_reply", send)
    monkeypatch.setattr(remarketing, "finish_remarketing_attempt", finish)

    result = await remarketing.run_remarketing_batch()

    assert result == {"claimed": 1, "sent": 0, "failed": 0}
    send.assert_not_called()
    finish.assert_not_called()


def test_sendability_requires_current_processing_lease_and_no_new_inbound(monkeypatch):
    import app.learning.remarketing as remarketing

    claimed_at = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"?column?": 1}
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://test"),
    )
    monkeypatch.setattr(remarketing, "get_conn", connection)

    assert remarketing.remarketing_attempt_is_sendable(14, claimed_at=claimed_at) is True
    sql, params = cursor.execute.call_args.args
    normalized = " ".join(sql.split())
    assert "attempt.status = 'processing'" in normalized
    assert "conversation.status = 'active'" in normalized
    assert "contact.marketing_status = 'eligible'" in normalized
    assert "contact.last_customer_message_at <= attempt.claimed_at" in normalized
    assert "attempt.claimed_at = %(claimed_at)s" in normalized
    assert params["attempt_id"] == 14
    assert params["claimed_at"] == claimed_at


def test_new_inbound_cancels_old_cycle_and_schedules_only_a_fresh_cycle(monkeypatch):
    import app.learning.remarketing as remarketing

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = (
        {"id": 30, "marketing_status": "eligible"},
        {"id": 40},
        {"id": 41},
    )
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            remarketing_meta_window_hours=24,
        ),
    )
    monkeypatch.setattr(remarketing, "get_remarketing_touch_hours", lambda _settings: [1, 12, 23])
    monkeypatch.setattr(remarketing, "get_conn", connection)

    remarketing.sync_remarketing_interaction(
        IncomingMessage(
            provider="brevo",
            channel="whatsapp",
            sender_key="whatsapp:customer-1",
            sender_phone="5511999999999",
            text="Quero ver outro relogio",
        ),
        inbound_id=101,
        response_metadata={
            "domain": "commerce",
            "commerce_state": {
                "active_product": {"name": "Relogio novo"},
            },
        },
    )

    statements = [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list]
    old_cycle_cancel = next(
        sql for sql in statements if "completion_reason = 'customer_reengaged'" in sql
    )
    old_attempt_cancel = next(
        sql
        for sql in statements
        if "UPDATE public.ai_remarketing_attempts" in sql
        and "status IN ('pending', 'processing', 'failed')" in sql
    )
    fresh_cycle = next(
        sql for sql in statements if "INSERT INTO public.ai_conversation_statuses" in sql
    )
    touch_inserts = [
        call
        for call in cursor.execute.call_args_list
        if "INSERT INTO public.ai_remarketing_attempts" in call.args[0]
    ]

    assert "WHERE id = %(active_id)s AND status = 'active'" in old_cycle_cancel
    assert "conversation_status_id = %(active_id)s" in old_attempt_cancel
    assert "RETURNING id" in fresh_cycle
    assert len(touch_inserts) == 3
    assert {call.args[1]["active_id"] for call in touch_inserts} == {41}
    assert [call.args[1]["touch_number"] for call in touch_inserts] == [1, 2, 3]
    normalized_touch_sql = " ".join(touch_inserts[0].args[0].split())
    assert (
        "WHEN ai_remarketing_attempts.status IN ('sent', 'processing') "
        "THEN ai_remarketing_attempts.scheduled_at"
    ) in normalized_touch_sql


def test_opt_out_cancels_processing_touch_without_starting_new_cycle(monkeypatch):
    import app.learning.remarketing as remarketing

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = (
        {"id": 31, "marketing_status": "opted_out"},
        {"id": 42},
    )
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(
        remarketing,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            remarketing_meta_window_hours=24,
        ),
    )
    monkeypatch.setattr(remarketing, "get_remarketing_touch_hours", lambda _settings: [1, 12, 23])
    monkeypatch.setattr(remarketing, "get_conn", connection)

    remarketing.sync_remarketing_interaction(
        IncomingMessage(
            provider="brevo",
            channel="whatsapp",
            sender_key="whatsapp:customer-2",
            text="SAIR",
        ),
        inbound_id=102,
        response_metadata={"domain": "commerce", "commerce_state": {}},
    )

    statements = [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list]
    assert any("completion_reason = %(reason)s" in sql for sql in statements)
    cancel_call = next(
        call
        for call in cursor.execute.call_args_list
        if "UPDATE public.ai_remarketing_attempts" in call.args[0]
    )
    assert "'processing'" in cancel_call.args[0]
    assert not any("INSERT INTO public.ai_conversation_statuses" in sql for sql in statements)
    status_call = next(
        call
        for call in cursor.execute.call_args_list
        if "completion_reason = %(reason)s" in call.args[0]
    )
    assert status_call.args[1]["reason"] == "customer_opted_out"
