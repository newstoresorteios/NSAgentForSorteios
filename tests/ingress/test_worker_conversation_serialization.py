import asyncio
from types import SimpleNamespace

import pytest

from app.ingress import worker


def row(number):
    return {"id": number, "payload_json": {"normalized": {
        "provider": "brevo", "channel": "whatsapp", "text": "test",
        "message_id": str(number), "conversation_id": "test-conversation",
        "sender_key": "test-sender",
    }}}


@pytest.mark.asyncio
async def test_same_conversation_workers_cannot_overlap(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(database_url=""))
    active = peak = 0
    completed = []

    async def process(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        completed.append(item["id"])
        active -= 1
        return {"ok": True}

    monkeypatch.setattr(worker, "_process_inbox_row_locked", process)
    await asyncio.gather(*(worker.process_inbox_row(row(i)) for i in range(3)))
    assert peak == 1
    assert completed == [0, 1, 2]


@pytest.mark.asyncio
async def test_failed_worker_releases_conversation_for_retry(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(database_url=""))

    async def process(item):
        if item["id"] == 1:
            raise RuntimeError("simulated failure")
        return {"ok": True}

    monkeypatch.setattr(worker, "_process_inbox_row_locked", process)
    with pytest.raises(RuntimeError):
        await worker.process_inbox_row(row(1))
    assert (await asyncio.wait_for(worker.process_inbox_row(row(2)), 1))["ok"]
