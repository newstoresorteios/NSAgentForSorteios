from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.product_photos import attach_presented_product_photos, outbound_catalog_photos
from app.ingress.outbox import build_outbound_envelope, result_from_outbox_row
from app.models import AgentResult, IncomingMessage

PHOTO = "https://images.tcdn.com.br/img/product.jpg"


def product_result():
    return AgentResult(reply_text="Encontrei uma possibilidade: Tissot PRX 35mm", intent="commerce",
        commercial_data={"products": [{"id": "10609", "name": "Tissot PRX 35mm", "image_url": PHOTO}]})


def test_final_product_photo_survives_outbox_without_extra_catalog_calls():
    incoming = IncomingMessage(provider="meta", channel="instagram")
    result = attach_presented_product_photos(incoming, product_result())
    replay = result_from_outbox_row({"reply_payload": build_outbound_envelope(incoming, result)})
    assert outbound_catalog_photos(replay) == [PHOTO]


def test_candidate_not_presented_and_handoff_do_not_attach_photo():
    incoming = IncomingMessage(provider="meta", channel="instagram")
    for text, handoff in [("Qual cor você procura?", False), ("Tissot PRX 35mm", True)]:
        result = product_result()
        result.reply_text, result.handoff_required = text, handoff
        assert not outbound_catalog_photos(attach_presented_product_photos(incoming, result))


@pytest.mark.parametrize("url", ["http://images.tcdn.com.br/p.jpg", "https://tcdn.com.br.evil.test/p.jpg", "https://user:secret@images.tcdn.com.br/p.jpg", "https://127.0.0.1/p.jpg"])
def test_untrusted_photo_not_forwarded(url):
    result = product_result()
    result.response_metadata["outbound_image_urls"] = [url]
    assert not outbound_catalog_photos(result)


@pytest.mark.asyncio
async def test_photo_retry_does_not_repeat_acknowledged_text(monkeypatch):
    from app.channels import meta_instagram as meta
    import app.ingress.outbox as outbox
    incoming = IncomingMessage(provider="meta", channel="instagram")
    result = attach_presented_product_photos(incoming, product_result())
    payload = build_outbound_envelope(incoming, result)
    row = {"id": 7, "lease_owner": "first-worker", "reply_payload": payload}
    def checkpoint(context, key, receipt):
        payload.setdefault("delivery_parts", {})[key] = receipt
    monkeypatch.setattr(outbox, "record_delivery_part", checkpoint)
    send = AsyncMock(side_effect=[{"ok": True, "provider_response": {"message_id": "text-id"}}, {"ok": False, "error": "photo_failed"}])
    monkeypatch.setattr(meta, "_send_meta_instagram_message", send)
    failed = await meta.send_meta_instagram_reply(incoming, result_from_outbox_row(row))
    assert not failed["ok"] and failed["text_delivered"]
    assert set(payload["delivery_parts"]) == {"text"}
    row["lease_owner"] = "retry-worker"
    send.reset_mock(side_effect=True)
    send.return_value = {"ok": True, "provider_response": {"message_id": "image-id"}}
    success = await meta.send_meta_instagram_reply(incoming, result_from_outbox_row(row))
    assert success["ok"]
    assert send.await_count == 1
    assert send.call_args.kwargs["message"] == {"attachment": {"type": "image", "payload": {"url": PHOTO}}}
    assert success["media_messages"] == [{"message_id": "image-id", "url": PHOTO, "type": "image"}]


@pytest.mark.asyncio
async def test_text_only_path_unchanged(monkeypatch):
    from app.channels import meta_instagram as meta
    send = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(meta, "_send_meta_instagram_message", send)
    assert await meta.send_meta_instagram_reply(IncomingMessage(), AgentResult(reply_text="Olá")) == {"ok": True}
    assert "message" not in send.call_args.kwargs


@pytest.mark.asyncio
async def test_native_photo_payload_and_text_are_sent_to_same_recipient(monkeypatch):
    from types import SimpleNamespace
    from app.channels import meta_instagram as meta

    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 20
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: {"message_id": f"ack-{len(calls)}"})

    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.setattr(meta, "get_settings", lambda: SimpleNamespace(
        meta_page_access_token="IGAA-test", meta_ig_business_account_id="business-id"))
    incoming = IncomingMessage(provider="meta", channel="instagram", sender_external_id="recipient")
    result = attach_presented_product_photos(incoming, product_result())
    sent = await meta.send_meta_instagram_reply(incoming, result)
    assert sent["ok"] and len(calls) == 2
    for url, kwargs in calls:
        assert url == "https://graph.instagram.com/v21.0/me/messages"
        assert kwargs["headers"] == {"Authorization": "Bearer IGAA-test"}
        assert kwargs["json"]["recipient"] == {"id": "recipient"}
        assert "params" not in kwargs
    assert calls[0][1]["json"]["message"] == {"text": result.reply_text}
    assert calls[1][1]["json"]["message"] == {"attachment": {"type": "image", "payload": {"url": PHOTO}}}
    assert sent["media_messages"] == [{"message_id": "ack-2", "url": PHOTO, "type": "image"}]


@pytest.mark.parametrize("lease_exists", [True, False])
def test_delivery_checkpoint_requires_current_unexpired_lease(monkeypatch, lease_exists):
    import app.ingress.outbox as outbox
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"id": 7} if lease_exists else None
    monkeypatch.setattr(outbox, "get_conn", lambda: connection)
    monkeypatch.setattr(outbox, "to_jsonb", lambda value: value)
    context = {"id": 7, "owner": "worker", "parts": {}}
    receipt = {"message_id": "text-id"}
    if lease_exists:
        outbox.record_delivery_part(context, "text", receipt)
        assert context["parts"] == {"text": receipt}
    else:
        with pytest.raises(RuntimeError, match="outbox_delivery_lease_lost"):
            outbox.record_delivery_part(context, "text", receipt)
        assert not context["parts"]
    sql, params = cursor.execute.call_args.args
    assert "status = 'leased'" in sql and "lease_owner = %(owner)s" in sql
    assert "lease_expires_at > now()" in sql
    assert params == {"id": 7, "owner": "worker", "part": {"text": receipt}}
