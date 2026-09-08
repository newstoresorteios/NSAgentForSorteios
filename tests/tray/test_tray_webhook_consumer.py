import pytest

from app.tray.tray_webhook_consumer import (
    apply_tray_webhook_event,
    consume_tray_webhook_events,
)


@pytest.mark.asyncio
async def test_product_update_refreshes_index(monkeypatch):
    called = {}

    async def fake_refresh(product_id, *, client=None):
        called["product_id"] = product_id
        return {"ok": True, "product_id": product_id, "upserted": 1}

    monkeypatch.setattr(
        "app.tray.tray_webhook_consumer.refresh_index_product",
        fake_refresh,
    )
    result = await apply_tray_webhook_event(
        {"scope_name": "product_stock", "scope_id": "41", "act": "update"}
    )
    assert result["ok"] is True
    assert called["product_id"] == "41"


@pytest.mark.asyncio
async def test_order_event_confirms_pix(monkeypatch):
    called = {}

    async def fake_confirm(order_id, *, client=None):
        called["order_id"] = order_id
        return {"ok": True, "action": "confirmed", "tray_order_id": order_id}

    monkeypatch.setattr(
        "app.tray.tray_webhook_consumer.confirm_pix_for_tray_order",
        fake_confirm,
    )
    result = await apply_tray_webhook_event(
        {"scope_name": "order", "scope_id": "900", "act": "insert"}
    )
    assert result["action"] == "confirmed"
    assert called["order_id"] == "900"


@pytest.mark.asyncio
async def test_consume_advances_cursor(monkeypatch):
    saved = {}

    class FakeClient:
        async def list_webhook_events(self, *, limit=50, since_id=None):
            assert since_id == 4
            return {
                "success": True,
                "events": [
                    {
                        "id": 5,
                        "scope_name": "customer",
                        "scope_id": "1",
                        "act": "update",
                    }
                ],
            }

    monkeypatch.setattr(
        "app.tray.tray_webhook_consumer.load_webhook_cursor",
        lambda: 4,
    )
    monkeypatch.setattr(
        "app.tray.tray_webhook_consumer.save_webhook_cursor",
        lambda event_id: saved.update({"id": event_id}),
    )
    result = await consume_tray_webhook_events(client=FakeClient())
    assert result["ok"] is True
    assert result["processed"] == 1
    assert saved["id"] == 5
    assert result["results"][0]["action"] == "ignored"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("fake failure"), {"ok": False}])
@pytest.mark.parametrize("initial", [0, 10])
async def test_failed_event_preserves_contiguous_checkpoint(monkeypatch, failure, initial):
    from unittest.mock import AsyncMock, Mock
    from types import SimpleNamespace
    import app.tray.tray_webhook_consumer as consumer

    client = SimpleNamespace(list_webhook_events=AsyncMock(return_value={"events": [
        {"id": initial+3}, {"id": initial+1}, {"id": initial+2}]}))
    apply = AsyncMock(side_effect=[{"ok":True}, failure, {"ok":True}])
    save = Mock()
    monkeypatch.setattr(consumer, "load_webhook_cursor", lambda: initial)
    monkeypatch.setattr(consumer, "save_webhook_cursor", save)
    monkeypatch.setattr(consumer, "apply_tray_webhook_event", apply)
    result = await consumer.consume_tray_webhook_events(client=client)
    assert result["ok"] is False
    save.assert_called_once_with(initial+1)
    assert apply.await_count == 2
    client.list_webhook_events.assert_awaited_once_with(limit=50, since_id=initial)


@pytest.mark.asyncio
async def test_crash_before_checkpoint_replays_page(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from types import SimpleNamespace
    import app.tray.tray_webhook_consumer as consumer

    client = SimpleNamespace(list_webhook_events=AsyncMock(return_value={"events":[{"id":11}]}))
    apply = AsyncMock(return_value={"ok":True})
    save = Mock(side_effect=[RuntimeError("fake db fault"), None])
    monkeypatch.setattr(consumer,"load_webhook_cursor",lambda:10)
    monkeypatch.setattr(consumer,"save_webhook_cursor",save)
    monkeypatch.setattr(consumer,"apply_tray_webhook_event",apply)
    with pytest.raises(RuntimeError):
        await consumer.consume_tray_webhook_events(client=client)
    assert (await consumer.consume_tray_webhook_events(client=client))["ok"] is True
    assert apply.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, {"events":None}, {"events":[],"success":False}, {"events":[],"error":"upstream"}])
async def test_invalid_page_is_not_successful_empty_poll(monkeypatch, payload):
    from unittest.mock import AsyncMock, Mock
    from types import SimpleNamespace
    import app.tray.tray_webhook_consumer as consumer

    save = Mock()
    monkeypatch.setattr(consumer,"load_webhook_cursor",lambda:10)
    monkeypatch.setattr(consumer,"save_webhook_cursor",save)
    client = SimpleNamespace(list_webhook_events=AsyncMock(return_value=payload))
    result = await consumer.consume_tray_webhook_events(client=client)
    assert result["ok"] is False and result["cursor"] == 10
    save.assert_not_called()
