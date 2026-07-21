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
    assert openai_agent._needs_local_account_lookup(
        IncomingMessage(text="Quanto fica no Pix?")
    ) is False


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
