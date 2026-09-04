from app.verify.guardrails import default_safe_handoff, detect_human_support_request
from app.persona.site_knowledge import HUMAN_SUPPORT_MESSAGE, NS_SALES_WHATSAPP


def test_human_support_message_contains_whatsapp():
    assert NS_SALES_WHATSAPP in HUMAN_SUPPORT_MESSAGE
    assert "vendas" in HUMAN_SUPPORT_MESSAGE.lower()


def test_default_safe_handoff_promises_human_queue():
    reply = default_safe_handoff()
    assert "instantes" in reply.lower()
    assert "encaminh" in reply.lower()


def test_detect_human_support_request():
    assert detect_human_support_request("Quero falar com um atendente")
    assert detect_human_support_request("Qual o contato de vendas?")
    assert detect_human_support_request("quero falar com um ser humano")
    assert detect_human_support_request("estou falando com um robô")
    assert not detect_human_support_request("qual meu saldo")
