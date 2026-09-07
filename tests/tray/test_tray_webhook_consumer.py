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
