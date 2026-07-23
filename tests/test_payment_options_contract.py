import pytest

from app.commerce_context import CommerceConversationState
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
