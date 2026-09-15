from unittest.mock import AsyncMock
import pytest
from app.configuration.runtime import current_bundle, bind_bundle, reset_bundle
from app.commerce.commerce_context import CommerceConversationState
from app.commerce.checkout_service import checkout_capabilities, select_checkout_channel, visible_cart_url
from app.commerce.checkout_data_service import update_checkout_data
from app.commerce.order_service import create_order, prepare_order


@pytest.fixture
def site_policy():
    bundle = current_bundle()
    token = bind_bundle({**bundle, "values": {**bundle["values"], "checkoutMode": "site"}}, None)
    try:
        yield
    finally:
        reset_bundle(token)


def state():
    return CommerceConversationState(cart_session_id="session-test", cart_url="https://store.example/checkout?session=verified",
                                     checkout_channel_preference="whatsapp")


@pytest.mark.asyncio
async def test_site_policy_prevents_tax_document_collection_and_order_writes(site_policy):
    current = state()
    execute = AsyncMock(side_effect=AssertionError("No order write allowed in site mode"))
    results = [select_checkout_channel(current, "whatsapp"), update_checkout_data(current, {"cpf": "12345678900"}),
               await prepare_order(state=current, execute=execute), await create_order(state=current, execute=execute)]
    for result in results:
        assert current.cart_url in result.reply_text
        assert "CPF" not in result.reply_text
        assert result.response_metadata["checkout_channel_preference"] == "site"
        assert result.response_metadata["clear_pending_action"]
    assert current.checkout_draft.customer.cpf is None
    assert not checkout_capabilities(current)["whatsapp_order_supported"]
    assert visible_cart_url(current) == current.cart_url
    execute.assert_not_awaited()


def test_missing_cart_link_does_not_invent_checkout_url(site_policy):
    result = select_checkout_channel(CommerceConversationState(), "site")
    assert result.handoff_required
    assert result.safety_reason == "checkout_link_unavailable"
    assert "http" not in result.reply_text
