from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.ingress import dispatch


def test_dispatch_rejects_invalid_auth_without_reading_vault(monkeypatch):
    monkeypatch.setattr(dispatch, "_dispatch_secret", lambda _: (_ for _ in ()).throw(AssertionError("vault must not be read")))
    for value in (None,"Bearer x","Basic anything"):
        with pytest.raises(HTTPException) as error:
            dispatch.verify_queue_dispatch(value)
        assert error.value.status_code == 401


def test_dispatch_checks_secret(monkeypatch):
    monkeypatch.setattr(dispatch, "_dispatch_secret", lambda _: "a"*64)
    dispatch.verify_queue_dispatch("Bearer " + "a"*64)
    with pytest.raises(HTTPException) as error:
        dispatch.verify_queue_dispatch("Bearer " + "b"*64)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_dispatch_drains_both_queues_when_one_worker_fails(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url=None, agent_db_persona_enabled=False, agent_inbox_batch_size=3))
    inbox = AsyncMock(side_effect=RuntimeError("offline"))
    outbox = AsyncMock(return_value={"claimed":0})
    monkeypatch.setattr("app.ingress.worker.process_inbox_batch", inbox)
    monkeypatch.setattr("app.ingress.outbox_worker.process_outbox_batch", outbox)
    monkeypatch.setattr("app.ops.observability.log_exception", lambda *args:None)
    await dispatch.dispatch_pending_queues()
    inbox.assert_awaited_once_with(limit=3)
    outbox.assert_awaited_once_with(limit=3)
