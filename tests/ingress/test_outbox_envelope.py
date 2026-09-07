from app.ingress.outbox import build_outbound_envelope, incoming_from_outbox_row
from app.models import AgentResult, IncomingMessage


def test_outbox_envelope_keeps_phone_raw_and_media():
    incoming = IncomingMessage(
        provider="brevo",
        channel="whatsapp",
        sender_phone="5511999999999",
        sender_key="whatsapp:5511999999999",
        conversation_id="wa-1",
        text="oi",
        image_url="https://cdn.example/a.jpg",
        raw={"messageId": "m1", "from": "5511999999999"},
    )
    result = AgentResult(reply_text="olá", intent="commerce")
    envelope = build_outbound_envelope(incoming, result)
    assert envelope["incoming"]["sender_phone"] == "5511999999999"
    assert envelope["incoming"]["image_url"] == "https://cdn.example/a.jpg"
    assert envelope["incoming"]["raw"]["messageId"] == "m1"
    rebuilt = incoming_from_outbox_row(
        {
            "provider": "brevo",
            "channel": "whatsapp",
            "conversation_key": "wa-1",
            "sender_key": "whatsapp:5511999999999",
            "reply_payload": envelope,
        }
    )
    assert rebuilt.sender_phone == "5511999999999"
    assert rebuilt.image_url == "https://cdn.example/a.jpg"
    assert rebuilt.provider == "brevo"


def test_outbox_resend_keeps_original_provider_for_instagram():
    incoming = IncomingMessage(
        provider="brevo",
        channel="instagram",
        visitor_id="v1",
        text="oi",
    )
    envelope = build_outbound_envelope(
        incoming, AgentResult(reply_text="ok", intent="commerce")
    )
    rebuilt = incoming_from_outbox_row(
        {
            "provider": "brevo",
            "channel": "instagram",
            "reply_payload": envelope,
        }
    )
    assert rebuilt.provider == "brevo"
    assert rebuilt.channel == "instagram"
