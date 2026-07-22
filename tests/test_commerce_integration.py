from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, IncomingMessage
from app.context_builder import detect_customer_intents, gather_customer_facts, _primary_intent


def _settings(**overrides):
    values = {
        "openai_api_key": "",
        "openai_model": "gpt-test",
        "tray_adapter_url": "https://tray-adapter.test",
        "tray_adapter_token": "secret-that-must-not-leak",
        "app_name": "test",
        "dry_run": True,
        "environment": "test",
        "database_url": "",
        "brevo_api_key": "",
        "brevo_agent_id": "",
        "brevo_agent_email": "",
        "brevo_agent_name": "",
        "brevo_sender_number": "",
        "brevo_reply_mode": "dry_run",
        "brevo_webhook_secret": "",
        "audio_inbound_enabled": True,
        "audio_outbound_enabled": True,
        "supabase_url": "",
        "supabase_service_key": "",
        "max_reply_chars": 900,
        "admin_api_token": "admin-secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_commerce_intents_and_local_intents_remain_distinct():
    assert _primary_intent(detect_customer_intents("Vocês têm Tissot Seastar?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Tem estoque desse relógio?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Quanto custa?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Quanto fica no Pix?")) == "commerce"
    assert _primary_intent(detect_customer_intents("saldo")) == "balance"
    assert "commerce" not in detect_customer_intents("saldo do João")


@pytest.mark.asyncio
async def test_third_party_balance_remains_blocked(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(sender_phone="5511999999999", text="saldo do João"),
        {},
    )
    assert result.intent == "security_refusal"


@pytest.mark.asyncio
async def test_greeting_does_not_lookup_account_or_handoff(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr(openai_agent, "find_coupon_balance_by_phone", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("greeting must not lookup account")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="olá"), {})
    assert result.intent == "general"
    assert result.handoff_required is False
    assert result.reply_text


def test_commerce_facts_do_not_lookup_personal_balance(monkeypatch):
    def fail_account_lookup(*args, **kwargs):
        raise AssertionError("commerce must not query local account")

    monkeypatch.setattr("app.context_builder.find_coupon_balance_by_phone", fail_account_lookup)
    facts = gather_customer_facts(
        IncomingMessage(sender_phone="5511999999999", text="Vocês têm Tissot Seastar?"),
        {"found": True, "name": "Cliente"},
    )
    assert facts["primary_intent"] == "commerce"
    assert facts["account"] == {"found": False}
    assert facts["display_name"] == "Cliente"


@pytest.mark.asyncio
async def test_async_agent_does_not_preload_account_for_commerce(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr(openai_agent, "find_coupon_balance_by_phone", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not lookup account")))
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Quanto fica no Pix?"),
        {},
    )
    assert result is not None


@pytest.mark.asyncio
async def test_ean_is_commerce_not_third_party(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": []}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Tem o EAN 7611608287637?"),
        {},
    )
    assert result.intent == "commerce"
    assert result.safety_reason == "product_not_found"
    assert calls == [("search_products", {"query": "EAN 7611608287637", "limit": 3})]


@pytest.mark.asyncio
async def test_general_does_not_send_tray_tools_or_commercial_fallback(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr("app.commerce_router.execute_tool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("general must not call Tray")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="oi"), {})
    assert result.intent == "general"
    assert result.reply_text == "Ol\u00e1! Como posso ajudar?"
    assert "informa\u00e7\u00f5es da loja" not in result.reply_text


@pytest.mark.asyncio
async def test_stale_inbound_does_not_send_old_agent_reply(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "inbound_message_exists", lambda *args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda message: (True, 41))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *args: False)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda phone: {})
    monkeypatch.setattr(index, "process_incoming_message", lambda *args: _async_result("commerce"))
    monkeypatch.setattr(index, "send_brevo_reply", lambda *args: (_ for _ in ()).throw(AssertionError("stale reply must not send")))
    recorded = []
    monkeypatch.setattr(index, "insert_agent_response", lambda data: recorded.append(data))
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            response = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={"id": "stale-1", "conversationId": "conv-1", "from": "5511999999999", "text": "Tem Tissot?"},
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 200
    assert response.json()["skipped_reply"] is True
    assert recorded[0]["provider_send_ok"] is False
    assert recorded[0]["provider_response"]["reason"] == "stale_inbound"


async def _async_result(intent):
    return AgentResult(reply_text="ok", intent=intent, handoff_required=False)


@pytest.mark.asyncio
async def test_health_exposes_only_tray_flags(monkeypatch):
    import api.index as index

    settings = _settings()
    monkeypatch.setattr(index, "get_settings", lambda: settings)
    payload = await index.health()
    assert payload["tray_adapter_configured"] is True
    assert payload["tray_tools_enabled"] is True
    assert settings.tray_adapter_token not in str(payload)
    assert settings.tray_adapter_url not in str(payload)


@pytest.mark.asyncio
async def test_tray_diagnostic_uses_client_and_is_admin_protected(monkeypatch):
    import api.index as index
    import app.security as security

    settings = _settings()
    monkeypatch.setattr(index, "get_settings", lambda: settings)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    calls = []

    class FakeTrayClient:
        async def search_products(self, **kwargs):
            calls.append(kwargs)
            return {"products": []}

    monkeypatch.setattr(index, "TrayAdapterClient", FakeTrayClient)
    from api.index import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/integrations/tray/test",
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "tray_adapter_connected": True,
        "products_accessible": True,
    }
    assert calls == [{"limit": 1}]


@pytest.mark.asyncio
async def test_brevo_duplicate_message_is_skipped_before_processing(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "inbound_message_exists", lambda provider, message_id: provider == "brevo" and message_id == "msg-1")
    monkeypatch.setattr(index, "insert_inbound_message", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate must not be inserted")))
    monkeypatch.setattr(index, "process_incoming_message", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate must not be processed")))
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            response = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={"id": "msg-1", "from": "5511999999999", "text": "olá"},
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": True, "reason": "duplicate_message"}
