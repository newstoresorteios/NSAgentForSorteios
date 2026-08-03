from app.commerce_context import CommerceConversationState
from app.observability import (
    redact_text,
    summarize_commerce_state,
    summarize_openai_messages,
    summarize_tray_result,
)


def test_redact_text_masks_pii_and_secrets():
    text = (
        "CPF 072.810.359-18 email tironinho@hotmail.com "
        "fone 85999498149 token sk-proj-abc123Bearer xyz"
    )
    redacted = redact_text(text)
    assert "072.810.359-18" not in redacted
    assert "tironinho@hotmail.com" not in redacted
    assert "85999498149" not in redacted
    assert "sk-proj-abc123" not in redacted
    assert "[CPF]" in redacted
    assert "[EMAIL]" in redacted


def test_summarize_commerce_and_openai_messages():
    state = CommerceConversationState(
        order_id="0CC131B51070AEF",
        pending_action="awaiting_payment",
        active_product={"product_id": "1", "name": "Seiko"},
        checkout_draft={"customer": {"cpf": "07281035918", "name": "Paulo"}},
    )
    summary = summarize_commerce_state(state)
    assert summary["has_order_id"] is True
    assert summary["order_id"] == "0CC131B51070AEF"
    assert "cpf" in summary["checkout_customer_fields"]
    assert "07281035918" not in str(summary["checkout_customer_fields"])

    messages = summarize_openai_messages(
        [
            {"role": "system", "content": "COMMERCE_STATE"},
            {"role": "user", "content": "meu cpf e 07281035918"},
        ]
    )
    assert messages[1]["preview"].find("[CPF]") >= 0


def test_summarize_tray_result_keeps_useful_flags():
    summary = summarize_tray_result(
        {
            "success": True,
            "order_id": "25400",
            "payment": {"status": "pending", "has_payment": False, "payment_url": "https://x"},
            "products": [{"id": 1}, {"id": 2}],
        }
    )
    assert summary["ok"] is True
    assert summary["order_id"] == "25400"
    assert summary["payment"]["payment_url_present"] is True
    assert summary["products_count"] == 2
