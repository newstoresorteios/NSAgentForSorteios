"""P0: URL/ref identity, material specs, discount objection, stale cart."""

from __future__ import annotations

import pytest

from app.commerce.cart_service import _is_stale_cart_session, stale_cart_session_result
from app.catalog.catalog_specs import (
    extract_material,
    reference_from_store_url,
)
from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage
from app.sales.policies.objection_authority import (
    detect_objection_kind,
    try_objection_authority_result,
)


def test_reference_from_store_url_certina_slug():
    url = (
        "https://www.newstorerj.com.br/relogios-certina/"
        "relogio-certina-ds-action-diver-powermatic-80-c032-807-44-081-00"
    )
    assert reference_from_store_url(url) == "C032.807.44.081.00"


def test_extract_material_titanium_from_title():
    product = {
        "name": "Certina DS Action Diver Powermatic 80 Titânio 38mm Cinza",
    }
    assert extract_material(product) == "titânio"


@pytest.mark.parametrize(
    "text",
    [
        "tem desconto?",
        "quero desconto",
        "no pix tem desconto?",
        "faz 20% no pix",
    ],
)
def test_detect_objection_extra_discount(text: str):
    assert detect_objection_kind(text) == "extra_discount"


def test_objection_wins_over_checkout_payment_kind():
    message = IncomingMessage(channel="whatsapp", text="tem desconto?")
    state = CommerceConversationState()
    from app.models import SalesInterpretation

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={},
        preferences={},
        information_needed=[],
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.8,
        payment_request_kind="checkout",
    )
    result = try_objection_authority_result(message, interpretation, state)
    assert result is not None
    assert "desconto" in result.reply_text.lower()
    assert "pix" in result.reply_text.lower()


def test_objection_does_not_steal_informational_payment_kind():
    message = IncomingMessage(channel="whatsapp", text="faz 20% no pix de desconto")
    state = CommerceConversationState()
    from app.models import SalesInterpretation

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="inspect",
        subject={},
        preferences={},
        information_needed=[],
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.8,
        payment_method_preference="pix",
        payment_request_kind="informational",
    )
    assert try_objection_authority_result(message, interpretation, state) is None


def test_stale_cart_session_detects_404():
    assert _is_stale_cart_session({"error": "not_found", "status_code": 404}) is True
    assert _is_stale_cart_session({"error": "timeout", "status_code": 503}) is False


def test_stale_cart_session_result_clears_metadata():
    state = CommerceConversationState(cart_session_id="abc123deadbeef")
    result = stale_cart_session_result(state=state, stage="cart_reconcile")
    cart_state = (result.response_metadata or {}).get("cart_state") or {}
    assert cart_state.get("cart_session_id") is None
    assert result.response_metadata.get("cart_session_cleared") is True
