from app.human_takeover import _candidate_keys
from app.models import IncomingMessage


def test_candidate_keys_dedupes():
    msg = IncomingMessage(
        provider="brevo",
        event_type="conversationFragment",
        channel="whatsapp",
        conversation_id="abc",
        sender_key="abc",
        sender_phone="5511999999999",
        visitor_id="vid",
        text="oi",
    )
    keys = _candidate_keys(msg)
    assert keys[0] == "abc"
    assert "5511999999999" in keys
    assert "vid" in keys
    assert len(keys) == len(set(keys))
