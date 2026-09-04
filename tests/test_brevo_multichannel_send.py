from types import SimpleNamespace

import pytest

from app.models import AgentResult, BrevoSendResult, IncomingMessage


def _settings(**overrides):
    values = {
        "brevo_api_key": "test-key",
        "brevo_agent_id": "agent-1",
        "brevo_agent_email": "",
        "brevo_agent_name": "NewStoreAgent",
        "brevo_received_from": "NewStoreAgent",
        "brevo_sender_number": "5511000000000",
        "brevo_send_url": "",
        "brevo_reply_mode": "auto",
        "brevo_send_audio_as_attachment": True,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_facebook_reply_uses_conversations_visitor_id_payload(monkeypatch):
    import app.channels.brevo_client as brevo

    captured = {}

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "outbound-1"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return Response()

    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())
    monkeypatch.setattr(brevo.httpx, "AsyncClient", Client)

    result = await brevo._send_conversations_reply(
        IncomingMessage(channel="facebook", visitor_id="brevo-visitor-fb"),
        "Segue o link oficial: https://loja.example/produto",
    )

    assert result.ok is True
    assert captured["url"] == brevo.BREVO_CONVERSATIONS_SEND_URL
    assert captured["json"] == {
        "text": "Segue o link oficial: https://loja.example/produto",
        "visitorId": "brevo-visitor-fb",
        "agentId": "agent-1",
    }


@pytest.mark.asyncio
async def test_social_channel_never_falls_into_whatsapp_transactional(monkeypatch):
    import app.channels.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings(brevo_reply_mode="whatsapp"))

    async def conversations(incoming, text, audio_file=None):
        calls.append(("conversations", incoming.visitor_id, text, audio_file))
        return BrevoSendResult(ok=True, dry_run=False)

    async def whatsapp(*_args):
        raise AssertionError("Facebook must not use the WhatsApp endpoint")

    monkeypatch.setattr(brevo, "_send_conversations_reply", conversations)
    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(
            channel="facebook",
            visitor_id="visitor-fb",
            sender_phone="5511999999999",
        ),
        AgentResult(reply_text="Olá"),
    )

    assert sent.ok is True
    assert calls == [("conversations", "visitor-fb", "Olá", None)]


@pytest.mark.asyncio
async def test_auto_whatsapp_keeps_transactional_number_route(monkeypatch):
    import app.channels.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())

    async def whatsapp(incoming, text):
        calls.append((incoming.sender_phone, text))
        return BrevoSendResult(ok=True, dry_run=False)

    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(channel="whatsapp", sender_phone="+55 11 99999-9999"),
        "Resposta",
    )

    assert sent.ok is True
    assert calls == [("+55 11 99999-9999", "Resposta")]


@pytest.mark.asyncio
async def test_whatsapp_uses_transactional_even_when_mode_is_conversations(monkeypatch):
    import app.channels.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings(brevo_reply_mode="conversations"))

    async def whatsapp(incoming, text):
        calls.append(("whatsapp", incoming.sender_phone, text))
        return BrevoSendResult(ok=True, dry_run=False, status_code=201)

    async def conversations(*_args, **_kwargs):
        raise AssertionError("WhatsApp with phone must not use Conversations when transactional succeeds")

    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)
    monkeypatch.setattr(brevo, "_send_conversations_reply", conversations)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(
            channel="whatsapp",
            sender_phone="5585999498149",
            visitor_id="visitor-wa",
        ),
        "Olá do Crono",
    )

    assert sent.ok is True
    assert calls == [("whatsapp", "5585999498149", "Olá do Crono")]
    assert sent.provider_response["route"] == "whatsapp_transactional"


@pytest.mark.asyncio
async def test_whatsapp_falls_back_to_conversations_when_transactional_fails(monkeypatch):
    import app.channels.brevo_client as brevo

    calls = []
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings(brevo_reply_mode="conversations"))

    async def whatsapp(*_args):
        calls.append("whatsapp")
        return BrevoSendResult(
            ok=False,
            dry_run=False,
            error="brevo_sender_number_missing",
            provider_response={"detail": "missing sender"},
        )

    async def conversations(incoming, text, audio_file=None):
        calls.append(("conversations", incoming.visitor_id, text, audio_file))
        return BrevoSendResult(
            ok=True,
            dry_run=False,
            status_code=200,
            provider_response={"id": "conv-1", "type": "agent"},
        )

    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)
    monkeypatch.setattr(brevo, "_send_conversations_reply", conversations)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(
            channel="whatsapp",
            sender_phone="5585999498149",
            visitor_id="visitor-wa",
        ),
        "Fallback",
    )

    assert sent.ok is True
    assert calls == ["whatsapp", ("conversations", "visitor-wa", "Fallback", None)]
    assert sent.provider_response["route"] == "brevo_conversations_fallback"
    assert sent.provider_response["transactional_error"] == "brevo_sender_number_missing"


def test_truncated_brevo_send_url_rewrites_to_whatsapp_path():
    from app.channels.brevo_client import (
        BREVO_WHATSAPP_SEND_URL,
        resolved_brevo_whatsapp_send_url,
    )

    empty, empty_reason = resolved_brevo_whatsapp_send_url("")
    assert empty == BREVO_WHATSAPP_SEND_URL
    assert empty_reason is None

    truncated, truncated_reason = resolved_brevo_whatsapp_send_url(
        "https://api.brevo.com/v3"
    )
    assert truncated == BREVO_WHATSAPP_SEND_URL
    assert truncated_reason == "truncated_api_root"

    slash, slash_reason = resolved_brevo_whatsapp_send_url("https://api.brevo.com/v3/")
    assert slash == BREVO_WHATSAPP_SEND_URL
    assert slash_reason == "truncated_api_root"

    conversations, conv_reason = resolved_brevo_whatsapp_send_url(
        "https://api.brevo.com/v3/conversations/messages"
    )
    assert conversations == BREVO_WHATSAPP_SEND_URL
    assert conv_reason == "missing_whatsapp_path"

    kept, keep_reason = resolved_brevo_whatsapp_send_url(BREVO_WHATSAPP_SEND_URL)
    assert kept == BREVO_WHATSAPP_SEND_URL
    assert keep_reason is None

    custom, custom_reason = resolved_brevo_whatsapp_send_url(
        "https://proxy.internal/whatsapp/sendMessage"
    )
    assert custom == "https://proxy.internal/whatsapp/sendMessage"
    assert custom_reason is None


def test_audio_outbound_ready_requires_storage():
    from app.config import audio_outbound_ready, supabase_storage_configured

    missing = SimpleNamespace(
        audio_outbound_enabled=True,
        supabase_url="",
        supabase_service_key="",
    )
    ready = SimpleNamespace(
        audio_outbound_enabled=True,
        supabase_url="https://example.supabase.co",
        supabase_service_key="service-key",
    )
    assert supabase_storage_configured(missing) is False
    assert audio_outbound_ready(missing) is False
    assert supabase_storage_configured(ready) is True
    assert audio_outbound_ready(ready) is True
    assert audio_outbound_ready(
        SimpleNamespace(
            audio_outbound_enabled=False,
            supabase_url="https://example.supabase.co",
            supabase_service_key="service-key",
        )
    ) is False


@pytest.mark.asyncio
async def test_whatsapp_transactional_posts_to_sendmessage_when_env_is_v3_root(monkeypatch):
    import app.channels.brevo_client as brevo

    captured = {}

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"messageId": "wa-1"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured["url"] = url
            return Response()

    monkeypatch.setattr(
        brevo,
        "get_settings",
        lambda: _settings(brevo_send_url="https://api.brevo.com/v3"),
    )
    monkeypatch.setattr(brevo.httpx, "AsyncClient", Client)

    result = await brevo._send_whatsapp_transactional_reply(
        IncomingMessage(channel="whatsapp", sender_phone="5511999999999"),
        "Olá",
    )

    assert result.ok is True
    assert captured["url"] == brevo.BREVO_WHATSAPP_SEND_URL


@pytest.mark.asyncio
async def test_audio_outbound_skipped_without_supabase_storage(monkeypatch):
    import app.message_pipeline as pipeline

    def boom(*_a, **_k):
        raise AssertionError("TTS must not run without storage")

    monkeypatch.setattr(
        pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            audio_outbound_enabled=True,
            supabase_url="",
            supabase_service_key="",
        ),
    )
    monkeypatch.setattr("app.channels.audio_service.synthesize_reply_audio", boom)

    result = await pipeline.enrich_agent_result(
        IncomingMessage(input_modality="audio", text="oi"),
        AgentResult(reply_text="Olá"),
    )
    assert result.reply_modality == "text"
    assert not result.reply_audio_url


def test_customer_visible_reply_text_strips_audio_placeholder():
    from app.channels.brevo_client import customer_visible_reply_text

    assert customer_visible_reply_text("Resposta em áudio") != "Resposta em áudio"
    assert "áudio" not in customer_visible_reply_text("").casefold() or "faixa" in customer_visible_reply_text("").casefold()
    assert customer_visible_reply_text("", audio_url="https://cdn.example/a.ogg") == "Ouça: https://cdn.example/a.ogg"
    assert customer_visible_reply_text("Olá") == "Olá"


@pytest.mark.asyncio
async def test_empty_reply_never_sends_audio_placeholder(monkeypatch):
    import app.channels.brevo_client as brevo

    captured = {}
    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())

    async def whatsapp(_incoming, text):
        captured["tx"] = text
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_send_failed", status_code=404)

    async def conversations(_incoming, text, audio_file=None):
        captured["conv"] = text
        return BrevoSendResult(ok=True, dry_run=False, status_code=200)

    monkeypatch.setattr(brevo, "_send_whatsapp_transactional_reply", whatsapp)
    monkeypatch.setattr(brevo, "_send_conversations_reply", conversations)

    sent = await brevo.send_brevo_reply(
        IncomingMessage(
            channel="whatsapp",
            sender_phone="5585999498149",
            visitor_id="visitor-wa",
        ),
        AgentResult(reply_text="", reply_modality="audio"),
    )

    assert sent.ok is True
    assert captured["tx"]
    assert captured["tx"] != "Resposta em áudio"
    assert captured["conv"] != "Resposta em áudio"
    assert "faixa de investimento" in captured["tx"].casefold()


@pytest.mark.asyncio
async def test_conversations_empty_text_never_uses_audio_caption(monkeypatch):
    import app.channels.brevo_client as brevo

    captured = {}

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "outbound-empty"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured.update(json)
            return Response()

    monkeypatch.setattr(brevo, "get_settings", lambda: _settings())
    monkeypatch.setattr(brevo.httpx, "AsyncClient", Client)

    result = await brevo._send_conversations_reply(
        IncomingMessage(channel="whatsapp", visitor_id="visitor-wa"),
        "",
    )

    assert result.ok is True
    assert captured["text"] != "Resposta em áudio"
    assert "faixa de investimento" in captured["text"].casefold()
