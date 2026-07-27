import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.payment_service import inspect_payment_options
from app.tray_tools import execute_tool


def _adapter_payload():
    plots = [
        {
            "installments": count,
            "value": value,
            "interest": int(count > 2),
            "interest_value": "4.50" if count > 2 else "0.00",
            "discount_value": "0.00",
            "base_value": "1200.00",
            "order_total": total,
        }
        for count, value, total in (
            (1, "1100.00", "1100.00"),
            (2, "600.00", "1200.00"),
            (10, "125.00", "1250.00"),
            (12, "108.33", "1299.96"),
        )
    ]
    return {
        "payment_options": [
            {
                "id": "P1",
                "name": "Pagamento instantâneo",
                "text": "Pague com Pix",
                "card": 0,
                "discount_value": "100.00",
                "increase_value": "0.00",
                "total_base": "1100.00",
                "tax_value": "0.00",
                "plots": [plots[0]],
            },
            {
                "id": "C1",
                "name": "Crédito",
                "text": "Cartão de crédito",
                "card": 1,
                "discount_value": "0.00",
                "increase_value": "50.00",
                "total_base": "1200.00",
                "tax_value": "50.00",
                "plots": plots,
            },
        ]
    }


class PaymentAdapter:
    async def get_payment_options(self, cart_session_id):
        assert cart_session_id == "SESSION"
        return _adapter_payload()


@pytest.mark.asyncio
async def test_real_payment_contract_recognizes_pix_card_and_all_plots():
    result = await execute_tool(
        "get_payment_options",
        {"cart_session_id": "SESSION"},
        PaymentAdapter(),
    )
    options = result["payment_options"]

    assert options["pix"]["id"] == "P1"
    assert options["card"]["id"] == "C1"
    assert [plot["count"] for plot in options["installments"]] == [1, 2, 10, 12]
    assert options["installments"][2] == {
        "count": 10,
        "value": 125.0,
        "interest": True,
        "interest_value": 4.5,
        "discount_value": 0.0,
        "base_value": 1200.0,
        "order_total": 1250.0,
    }


@pytest.mark.asyncio
async def test_payment_service_uses_exact_ten_installment_plot():
    normalized = await execute_tool(
        "get_payment_options",
        {"cart_session_id": "SESSION"},
        PaymentAdapter(),
    )

    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            assert arguments == {"session_id": "SESSION"}
            return {"items": [], "total": "1250.00"}
        assert tool == "get_payment_options"
        assert arguments == {"cart_session_id": "SESSION"}
        return normalized
    result = await inspect_payment_options(
        state=CommerceConversationState(
            cart_session_id="SESSION",
            cart_url="https://loja.example/checkout/SESSION",
        ),
        installment_count=10,
        payment_method_preference="card",
        execute=execute,
    )

    assert result.commercial_data["requested_method_available"] is True
    assert result.commercial_data["requested_installment"]["count"] == 10
    assert result.commercial_data["requested_installment"]["value"] == 125.0
    assert result.commercial_data["requested_installment"]["order_total"] == 1250.0
    assert result.response_metadata["selected_payment_method"] == "card"
    assert result.response_metadata["selected_payment_option_id"] == "C1"
    assert result.response_metadata["pending_action"] == "choose_checkout_channel"


@pytest.mark.asyncio
async def test_pix_selection_uses_factual_gateway_name_and_persists_option_details():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [], "total": "10.00"}
        assert tool == "get_payment_options"
        return {"payment_options": {
            "pix": {
                "id": "PIX-XPTO",
                "name": "Pix - Gateway XPTO",
                "discount_value": 2.5,
                "plots": [{"count": 1, "value": 97.5}],
            },
            "options": [{"id": "PIX-XPTO", "name": "Pix - Gateway XPTO"}],
        }}

    previous = CommerceConversationState(cart_session_id="SESSION")
    result = await inspect_payment_options(
        state=previous,
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
    )
    current = evolve_commerce_state(previous, result)

    assert result.commercial_data["payment_method"]["name"] == "Pix - Gateway XPTO"
    assert current.selected_payment_option.name == "Pix - Gateway XPTO"
    assert current.selected_payment_option.installments == [{"count": 1, "value": 97.5}]
    assert current.selected_payment_option.discount_value == 2.5


@pytest.mark.asyncio
async def test_unavailable_pix_does_not_select_another_factual_option():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [], "total": "10.00"}
        assert tool == "get_payment_options"
        return {"payment_options": {
            "boleto": {"id": "B1", "name": "Boleto - Gateway XPTO"},
            "options": [{"id": "B1", "name": "Boleto - Gateway XPTO"}],
        }}

    result = await inspect_payment_options(
        state=CommerceConversationState(cart_session_id="SESSION"),
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
    )

    assert result.safety_reason == "payment_method_unavailable"
    assert result.commercial_data["payment_method"] == {
        "type": "pix", "name": None, "available": False,
    }
    assert "selected_payment_option" not in result.response_metadata
