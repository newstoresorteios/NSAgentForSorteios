from app.webhook_parser import parse_brevo_whatsapp_payload


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
