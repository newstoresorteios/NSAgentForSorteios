import pytest

from app.models import IncomingMessage
from app.verify.guardrails import detect_raffle_group_inquiry
from app.persona.site_knowledge import build_raffle_group_reply


def test_detects_group_access_questions_from_real_conversation():
    assert detect_raffle_group_inquiry("Como entro no grupo ?") is True
    assert detect_raffle_group_inquiry("manda o link do grupo") is True
    assert detect_raffle_group_inquiry("qual é o grupo do sorteio?") is True


def test_does_not_route_unrelated_group_word():
    assert detect_raffle_group_inquiry("esse grupo de relógios é bonito") is False


def test_group_reply_contains_official_group_and_site_links():
    reply = build_raffle_group_reply()
    assert "https://chat.whatsapp.com/GdosYmyW2Jj1mDXNDTFt6F" in reply
    assert "https://www.sorteionewstore.com.br/" in reply


@pytest.mark.asyncio
async def test_real_customer_phrase_uses_local_group_route_without_handoff(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr("app.agents.door.load_recent_conversation_turns", lambda **_: [])
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Como entro no grupo ?", conversation_id="ig:test"),
        {},
    )
    assert result.intent == "raffle_group"
    assert result.handoff_required is False
    assert "https://chat.whatsapp.com/GdosYmyW2Jj1mDXNDTFt6F" in result.reply_text
