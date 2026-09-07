import pytest

from app.ingress.busy import (
    LOCK_DEFERRED_RETRY_SECONDS,
    drain_lock_deferred_inbound,
    inbox_drain_keys,
)
from app.models import IncomingMessage


def test_hobby_lock_timeout_stays_at_15_seconds():
    from app.config import get_settings

    settings = get_settings()
    assert settings.agent_conversation_lock_timeout_seconds == 15.0
    assert LOCK_DEFERRED_RETRY_SECONDS <= 2.0


def test_inbox_drain_keys_keep_lock_and_sender():
    incoming = IncomingMessage(
        text="que pedi a imagem",
        channel="whatsapp",
        conversation_id="thread-1",
        sender_key="whatsapp:5543999999999",
        sender_phone="5543999999999",
    )
    keys = inbox_drain_keys(lock_key="phone:5543999999999", incoming=incoming)
    assert keys[0] == "phone:5543999999999"
    assert "thread-1" in keys
    assert "whatsapp:5543999999999" in keys


@pytest.mark.asyncio
async def test_drain_gives_up_when_lock_still_busy(monkeypatch):
    from app.ops.conversation_lock import ConversationLockUnavailable

    async def busy(*_args, **_kwargs):
        raise ConversationLockUnavailable("local_lock_timeout")

    monkeypatch.setattr(
        "app.ops.conversation_lock.acquire_conversation_lock",
        busy,
    )
    claimed = {"n": 0}

    def must_not_claim(**_kwargs):
        claimed["n"] += 1
        raise AssertionError("must not lease inbox while the lock is busy")

    monkeypatch.setattr("app.ingress.inbox.claim_pending_inbox", must_not_claim)
    result = await drain_lock_deferred_inbound(
        conversation_keys=["phone:5543999999999"],
        database_url="",
    )
    assert result == {"ok": True, "claimed": 0, "deferred": True}
    assert claimed["n"] == 0


@pytest.mark.asyncio
async def test_drain_processes_pending_rows_for_the_same_conversation(monkeypatch):
    handle = type("H", (), {"released": False})()

    async def acquire(*_args, **_kwargs):
        return handle

    released = {"n": 0}

    async def release(value):
        released["n"] += 1
        assert value is handle

    rows = [{"id": 11, "payload_json": {}}]
    monkeypatch.setattr(
        "app.ops.conversation_lock.acquire_conversation_lock",
        acquire,
    )
    monkeypatch.setattr(
        "app.ops.conversation_lock.release_conversation_lock",
        release,
    )
    monkeypatch.setattr(
        "app.ingress.inbox.claim_pending_inbox",
        lambda **kwargs: rows if "phone:1" in (kwargs.get("conversation_keys") or []) else [],
    )

    async def process_row(row):
        return {"ok": True, "inbox_id": row["id"]}

    monkeypatch.setattr("app.ingress.worker.process_inbox_row", process_row)
    result = await drain_lock_deferred_inbound(
        conversation_keys=["phone:1"],
        database_url="",
    )
    assert result["claimed"] == 1
    assert result["processed"] == 1
    assert released["n"] == 1
