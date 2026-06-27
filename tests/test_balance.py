from app.guardrails import detect_balance_inquiry
from app.repository import format_cents_to_brl


def test_detect_balance_inquiry():
    assert detect_balance_inquiry("Quero saber meu saldo?") is True
    assert detect_balance_inquiry("Ola") is False


def test_format_cents_to_brl():
    assert format_cents_to_brl(15050) == "R$ 150,50"
    assert format_cents_to_brl(100000) == "R$ 1.000,00"
    assert format_cents_to_brl(None) == "R$ 0,00"
