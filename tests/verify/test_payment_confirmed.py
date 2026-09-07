from app.verify.factual_validator import _payment_confirmed


def test_payment_confirmed_accepts_explicit_paid_states():
    assert _payment_confirmed("paid") is True
    assert _payment_confirmed("approved") is True
    assert _payment_confirmed("pago") is True
    assert _payment_confirmed("aprovado") is True
    assert _payment_confirmed("payment_approved") is True


def test_payment_confirmed_rejects_negated_substrings():
    assert _payment_confirmed("unpaid") is False
    assert _payment_confirmed("not approved") is False
    assert _payment_confirmed("não pago") is False
    assert _payment_confirmed("nao pago") is False
    assert _payment_confirmed("pending unpaid") is False
    assert _payment_confirmed("awaiting_payment") is False


def test_payment_confirmed_unknown_is_none():
    assert _payment_confirmed(None) is None
    assert _payment_confirmed("") is None
    assert _payment_confirmed("unknown") is None
