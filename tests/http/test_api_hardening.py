"""Public health, webhook payload, persist-after-send, Meta enqueue, cron GET."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, BrevoSendResult


def _cron_settings():
    return SimpleNamespace(remarketing_cron_secret="cron-secret")


def _webhook_settings(**overrides):
    values = {
        "brevo_webhook_secret": "s3cret",
        "environment": "test",
        "agent_runtime_enabled": False,
        "agent_http_obs_logs": False,
        "admin_api_token": "admin-secret",
        "app_name": "test",
        "dry_run": True,
        "database_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _post_brevo(app, payload, *, headers=None, content=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if content is not None:
            return await client.post(
                "/api/webhooks/brevo/whatsapp",
                content=content,
                headers=headers or {"content-type": "text/plain"},
            )
        return await client.post(
            "/api/webhooks/brevo/whatsapp",
            json=payload,
            headers=headers,
        )


@pytest.mark.asyncio
async def test_public_health_http_is_slim(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "get_settings", lambda: _webhook_settings())
    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/health")
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["agent_version"]
    assert body["deployment_sha"] == "unknown"
    assert "openai_key_length" not in body
    assert "tray_adaptor_probe" not in body
    assert "raw_preview" not in body


@pytest.mark.asyncio
async def test_public_health_exposes_short_deployment_sha(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "get_settings", lambda: _webhook_settings())
    monkeypatch.setenv(
        "VERCEL_GIT_COMMIT_SHA",
        "bd7be8fb2aec43cdd0b364744422d69e26a19b7f",
    )
    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/health")
    assert response.json()["deployment_sha"] == "bd7be8fb2aec"


@pytest.mark.asyncio
async def test_invalid_json_has_no_raw_preview(monkeypatch):
    import api.index as index

    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        response = await _post_brevo(
            index.app,
            None,
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 400
    dumped = response.text
    assert "raw_preview" not in dumped
    assert "{not-json" not in dumped


@pytest.mark.asyncio
async def test_brevo_query_token_compatibility_and_header_precedence(monkeypatch):
    import api.index as index
    import app.security as security

    settings = _webhook_settings()
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        query_accepted = await client.post(
            "/api/webhooks/brevo/whatsapp?token=s3cret",
            json={"eventName": "conversationTranscript"},
        )
        header_accepted = await client.post(
            "/api/webhooks/brevo/whatsapp",
            json={"eventName": "conversationTranscript"},
            headers={"X-Webhook-Token": "s3cret"},
        )
        invalid_query = await client.post(
            "/api/webhooks/brevo/whatsapp?token=wrong",
            json={"eventName": "conversationTranscript"},
        )
        invalid_header_wins = await client.post(
            "/api/webhooks/brevo/whatsapp?token=s3cret",
            json={"eventName": "conversationTranscript"},
            headers={"X-Webhook-Token": "wrong"},
        )
        duplicate_query = await client.post(
            "/api/webhooks/brevo/whatsapp?token=s3cret&token=s3cret",
            json={"eventName": "conversationTranscript"},
        )
    assert query_accepted.status_code == 200
    assert query_accepted.json()["skipped"] is True
    assert header_accepted.status_code == 200
    assert header_accepted.json()["skipped"] is True
    assert invalid_query.status_code == 401
    assert invalid_header_wins.status_code == 401
    assert duplicate_query.status_code == 401


@pytest.mark.asyncio
async def test_persist_failure_after_send_returns_200(monkeypatch):
    import api.index as index

    async def process(*_args):
        return AgentResult(reply_text="ok", intent="commerce", handoff_required=False)

    async def send(*_args):
        return BrevoSendResult(ok=True, dry_run=True, status_code=200)

    monkeypatch.setattr(index, "inbound_already_completed", lambda *_a, **_k: False)
    monkeypatch.setattr(index, "inbound_message_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda *_a, **_k: (True, 88))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *_a, **_k: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda *_a, **_k: {})
    monkeypatch.setattr(index, "process_incoming_message", process)
    monkeypatch.setattr(index, "send_brevo_reply", send)
    monkeypatch.setattr(
        index,
        "insert_agent_response",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        response = await _post_brevo(
            index.app,
            {
                "id": "persist-1",
                "conversationId": "conv-1",
                "from": "5511999999999",
                "text": "Tem Tissot?",
            },
        )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply_sent"] is True
    assert body["persist_warning"] == "response_insert_failed"


@pytest.mark.asyncio
async def test_meta_webhook_enqueues_without_inline_worker(monkeypatch):
    import api.index as index
    from app.channels import meta_instagram
    from app.ingress import inbox
    from app.ingress import worker

    monkeypatch.setattr(meta_instagram, "meta_webhook_enabled", lambda: True)
    monkeypatch.setattr(meta_instagram, "verify_meta_signatures", lambda **_kw: True)
    queued: list[dict] = []

    def enqueue(**kwargs):
        queued.append(kwargs)
        return True, 9

    monkeypatch.setattr(inbox, "enqueue_inbound", enqueue)

    async def batch(**_kwargs):
        raise AssertionError("inline worker must not run")

    monkeypatch.setattr(worker, "process_inbox_batch", batch)

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-biz",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "sender": {"id": "user-2"},
                            "recipient": {"id": "ig-biz"},
                            "message": {"mid": "m2", "text": "teste"},
                        },
                    }
                ],
            }
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/webhooks/meta", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["messages"] == 1
    assert queued
    assert "worker" not in body


@pytest.mark.asyncio
async def test_catalog_url_health_get_ignores_clear_brand_cache(monkeypatch):
    import api.index as index
    import app.security as security

    monkeypatch.setattr(security, "get_settings", lambda: _cron_settings())

    async def repair(**_kwargs):
        return {"ok": True, "repaired": 0}

    async def warm(*_args, **_kwargs):
        return {"warmed": 0}

    monkeypatch.setattr(
        "app.catalog.media.url_health.repair_catalog_storefront_urls",
        repair,
    )
    monkeypatch.setattr(
        "app.catalog.media.url_health.mark_stale_or_zero_price_unavailable",
        lambda **_k: {"marked": 0},
    )
    monkeypatch.setattr(
        "app.catalog.index.warm.refresh_top_brands_into_index",
        warm,
    )

    def boom(*_a, **_k):
        raise AssertionError("GET must not clear brand cache")

    monkeypatch.setattr("app.db.get_conn", boom)
    monkeypatch.setattr("app.catalog.index.warm.list_top_index_brands", boom)

    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/cron/catalog-url-health?clear_brand_cache=true",
            headers={"Authorization": "Bearer cron-secret"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_debug_echo_returns_keys_only(monkeypatch):
    import api.index as index
    import app.security as security

    monkeypatch.setattr(security, "get_settings", lambda: _webhook_settings())
    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/debug/echo",
            json={"secret": "must-not-echo", "phone": "5511"},
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["keys"] == ["secret", "phone"]
    assert "payload" not in body
    assert "must-not-echo" not in response.text
