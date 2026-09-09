"""Recovery guarantees for the immutable outbound outbox envelope."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.ingress import outbox, outbox_worker


def _row(*, attempts: int, max_attempts: int = 3) -> dict:
    return {
        "id": 41,
        "inbound_id": 91,
        "provider": "brevo",
        "channel": "whatsapp",
        "conversation_key": "conversation-91",
        "sender_key": "sender-91",
        "reply_text": "Resposta aceita e imutavel",
        "reply_payload": {
            "incoming": {
                "provider": "brevo",
                "channel": "whatsapp",
                "conversation_id": "conversation-91",
                "sender_key": "sender-91",
                "text": "",
            },
            "result": {
                "reply_text": "Resposta aceita e imutavel",
                "intent": "commerce",
            },
        },
        "lease_owner": f"worker-{attempts}",
        "attempts": attempts,
        "max_attempts": max_attempts,
    }


@pytest.mark.asyncio
async def test_retry_failure_then_recovery_records_one_success(monkeypatch):
    """A recovered envelope is sent once after success and is never regenerated."""
    import app.db as db

    claims = iter(([_row(attempts=1)], [_row(attempts=2)], []))
    monkeypatch.setattr(
        outbox_worker,
        "get_settings",
        lambda: SimpleNamespace(agent_inbox_batch_size=5),
    )
    monkeypatch.setattr(
        outbox_worker,
        "claim_pending_outbox",
        lambda **_kwargs: next(claims),
    )
    resend = AsyncMock(
        side_effect=(
            {"ok": False, "error": "provider_timeout"},
            {"ok": True, "provider_response": {"message_id": "msg-91"}},
        )
    )
    monkeypatch.setattr(outbox_worker, "_resend_outbox_row", resend)
    marked_failed = Mock()
    marked_sent = Mock()
    monkeypatch.setattr(outbox_worker, "mark_outbox_failed", marked_failed)
    monkeypatch.setattr(outbox_worker, "mark_outbox_sent", marked_sent)
    monkeypatch.setattr(db, "has_successful_agent_response", lambda _inbound_id: False)
    persisted = Mock()
    monkeypatch.setattr(db, "insert_agent_response", persisted)

    first = await outbox_worker.process_outbox_batch()
    second = await outbox_worker.process_outbox_batch()
    third = await outbox_worker.process_outbox_batch()

    assert first == {
        "ok": True,
        "claimed": 1,
        "sent": 0,
        "failed": 1,
        "dead": 0,
        "items": [{"id": 41, "status": "failed"}],
    }
    assert second["sent"] == 1
    assert third == {"ok": True, "claimed": 0, "sent": 0, "failed": 0, "dead": 0}
    assert resend.await_count == 2
    marked_failed.assert_called_once_with(
        41,
        error="provider_timeout",
        dead=False,
        owner="worker-1",
    )
    marked_sent.assert_called_once()
    assert marked_sent.call_args.kwargs["owner"] == "worker-2"
    persisted.assert_called_once()
    assert persisted.call_args.args[0]["provider_send_ok"] is True
    assert persisted.call_args.args[0]["reply_text"] == "Resposta aceita e imutavel"


@pytest.mark.asyncio
async def test_existing_success_audit_is_not_duplicated_on_recovery(monkeypatch):
    import app.db as db

    monkeypatch.setattr(
        outbox_worker,
        "get_settings",
        lambda: SimpleNamespace(agent_inbox_batch_size=5),
    )
    monkeypatch.setattr(
        outbox_worker,
        "claim_pending_outbox",
        lambda **_kwargs: [_row(attempts=2)],
    )
    monkeypatch.setattr(
        outbox_worker,
        "_resend_outbox_row",
        AsyncMock(return_value={"ok": True, "provider_response": {"message_id": "msg-91"}}),
    )
    monkeypatch.setattr(outbox_worker, "mark_outbox_sent", Mock())
    monkeypatch.setattr(db, "has_successful_agent_response", lambda _inbound_id: True)
    persisted = Mock()
    monkeypatch.setattr(db, "insert_agent_response", persisted)

    result = await outbox_worker.process_outbox_batch()

    assert result["sent"] == 1
    persisted.assert_not_called()


@pytest.mark.asyncio
async def test_last_failed_attempt_is_dead_lettered(monkeypatch):
    monkeypatch.setattr(
        outbox_worker,
        "get_settings",
        lambda: SimpleNamespace(agent_inbox_batch_size=5),
    )
    monkeypatch.setattr(
        outbox_worker,
        "claim_pending_outbox",
        lambda **_kwargs: [_row(attempts=3, max_attempts=3)],
    )
    monkeypatch.setattr(
        outbox_worker,
        "_resend_outbox_row",
        AsyncMock(side_effect=TimeoutError("provider did not answer")),
    )
    marked_failed = Mock()
    monkeypatch.setattr(outbox_worker, "mark_outbox_failed", marked_failed)

    result = await outbox_worker.process_outbox_batch()

    assert result["dead"] == 1
    assert result["failed"] == 0
    marked_failed.assert_called_once_with(
        41,
        error="TimeoutError",
        dead=True,
        owner="worker-3",
    )


def test_claim_dead_letters_stale_reply_before_retrying_any_row(monkeypatch):
    """A later inbound wins over a delayed reply from the same conversation."""
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(
        outbox,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://test"),
    )
    monkeypatch.setattr(outbox, "ensure_tables", lambda: None)
    monkeypatch.setattr(outbox, "get_conn", connection)

    assert outbox.claim_pending_outbox(owner="worker-test") == []

    assert cursor.execute.call_count == 2
    stale_sql = " ".join(cursor.execute.call_args_list[0].args[0].split())
    claim_sql = " ".join(cursor.execute.call_args_list[1].args[0].split())
    assert "SET status = 'dead'" in stale_sql
    assert "later.created_at > outbox.created_at" in stale_sql
    assert "later.sender_key = outbox.sender_key" in stale_sql
    assert "later.conversation_id = outbox.conversation_key" in stale_sql
    assert "outbox.created_at < now() - interval '15 minutes'" in stale_sql
    assert "UPDATE public.ai_outbound_outbox AS outbox SET status = 'leased'" in claim_sql
    assert cursor.execute.call_args_list[1].args[1]["owner"] == "worker-test"
