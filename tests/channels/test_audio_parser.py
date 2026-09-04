import pytest

from app.channels.audio_service import extract_audio_attachment, is_audio_attachment
from app.channels.webhook_parser import parse_brevo_whatsapp_payload


def test_parse_audio_message_from_brevo_webhook():
    payload = {
        "eventName": "conversationFragment",
        "visitor": {"id": "visitor-1", "attributes": {"SMS": "5585999999999"}},
        "messages": [
            {
                "id": "msg-1",
                "type": "visitor",
                "file": {
                    "name": "audio.ogg",
                    "link": "https://cdn.example.com/audio.ogg",
                    "mimeType": "audio/ogg",
                },
            }
        ],
    }
    incoming = parse_brevo_whatsapp_payload(payload)
    assert incoming.input_modality == "audio"
    assert incoming.audio_url == "https://cdn.example.com/audio.ogg"
    assert incoming.audio_filename == "audio.ogg"
    assert is_audio_attachment(payload["messages"][0]["file"]) is True


def test_extract_audio_attachment():
    payload = {
        "messages": [
            {
                "type": "visitor",
                "file": {"name": "voice.opus", "link": "https://x/voice.opus", "mimeType": "audio/ogg"},
            }
        ]
    }
    audio = extract_audio_attachment(payload)
    assert audio is not None
    assert audio["link"] == "https://x/voice.opus"


@pytest.mark.asyncio
async def test_failed_audio_uses_fixed_copy_without_sync_legacy(monkeypatch):
    from app import openai_agent
    from app.models import IncomingMessage

    async def interpret_must_not_run(*_a, **_k):
        raise AssertionError("failed audio must not interpret")

    def sync_legacy_must_not_run(*_a, **_k):
        raise AssertionError("failed audio must not re-enter generate_agent_reply")

    monkeypatch.setattr(openai_agent, "interpret_message", interpret_must_not_run)
    monkeypatch.setattr(openai_agent, "generate_agent_reply", sync_legacy_must_not_run)
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(
        openai_agent,
        "should_request_human_handoff",
        lambda *_a, **_k: None,
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="",
            input_modality="audio",
            transcription_failed=True,
            conversation_id="audio-fail",
        ),
        {},
    )
    assert result.intent == "audio_transcription_failed"
    assert result.safety_reason == "audio_transcription_failed"
    assert "transcrever" in result.reply_text.casefold()
    assert result.response_metadata.get("fallback_reason") == "audio_transcription_failed"


def test_sync_legacy_failed_audio_does_not_open_tools(monkeypatch):
    from app import openai_agent
    from app.models import IncomingMessage

    def tools_must_not_run(*_a, **_k):
        raise AssertionError("failed audio must not open the tool loop")

    monkeypatch.setattr(openai_agent, "generate_openai_reply", tools_must_not_run)
    monkeypatch.setattr(openai_agent, "deterministic_scope", lambda _t: {"domain": "greeting"})

    result = openai_agent.generate_agent_reply(
        IncomingMessage(
            text="",
            input_modality="audio",
            transcription_failed=True,
        ),
        {},
    )
    assert result.intent == "audio_transcription_failed"
    assert result.safety_reason == "audio_transcription_failed"
