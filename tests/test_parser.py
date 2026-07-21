from app.webhook_parser import inbound_skip_reason, parse_brevo_whatsapp_payload, should_skip_auto_reply


def test_parse_basic_payload():
    msg = parse_brevo_whatsapp_payload({"from": "554399999999", "text": "Olá", "name": "Paulo"})
    assert msg.sender_phone == "554399999999"
    assert msg.text == "Olá"
    assert msg.sender_name == "Paulo"


def test_parse_messages_array_payload():
    payload = {"messages": [{"from": "554399999999", "text": {"body": "Oi"}, "id": "abc"}]}
    msg = parse_brevo_whatsapp_payload(payload)
    assert msg.sender_phone == "554399999999"
    assert msg.text == "Oi"
    assert msg.message_id == "abc"


def test_parse_conversations_fragment_payload():
    payload = {
        "eventName": "conversationFragment",
        "conversationId": "abc123",
        "messages": [
            {"type": "visitor", "id": "msg1", "text": "Ola"},
        ],
        "visitor": {
            "id": "visitor123",
            "displayedName": "Dark Orange",
            "attributes": {"SMS": "+55 85 99949 8149"},
        },
    }
    msg = parse_brevo_whatsapp_payload(payload)
    assert msg.event_type == "conversationFragment"
    assert msg.visitor_id == "visitor123"
    assert msg.sender_phone == "+55 85 99949 8149"
    assert msg.text == "Ola"


def test_should_skip_when_last_message_is_agent():
    payload = {
        "messages": [
            {"type": "visitor", "text": "Ola"},
            {"type": "agent", "text": "Oi"},
        ]
    }
    assert should_skip_auto_reply(payload) is True


def test_fragment_uses_latest_visitor_message_and_its_message_id():
    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conv-1",
        "messages": [
            {"type": "visitor", "id": "old", "text": "Mensagem antiga"},
            {"type": "agent", "id": "agent-1", "text": "Resposta anterior"},
            {"type": "visitor", "messageId": "new", "text": "Tem Tissot?"},
        ],
        "visitor": {"id": "visitor-1", "attributes": {"SMS": "5511999999999"}},
    }
    incoming = parse_brevo_whatsapp_payload(payload)
    assert incoming.message_id == "new"
    assert incoming.text == "Tem Tissot?"
    assert inbound_skip_reason(payload) is None


def test_fragment_with_latest_agent_is_skipped_as_agent_message():
    payload = {
        "eventName": "conversationFragment",
        "messages": [
            {"type": "visitor", "id": "visitor-1", "text": "Oi"},
            {"type": "agent", "id": "agent-1", "text": "Olá"},
        ],
    }
    assert inbound_skip_reason(payload) == "agent_message"
