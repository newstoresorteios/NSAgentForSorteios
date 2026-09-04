from app.llm.agent_contracts import build_agent_decision
from app.verify.factual_validator import (
    apply_factual_validation,
    validate_factual_response,
)
from app.models import AgentResult, IncomingMessage


def _decision(result: AgentResult):
    return build_agent_decision(
        IncomingMessage(
            channel="whatsapp",
            sender_key="whatsapp:test",
            text="consulta",
        ),
        result,
        openai_call_count=1,
    )


def test_verified_product_url_and_price_are_accepted():
    result = AgentResult(
        reply_text=(
            "O produto custa R$ 199,90. "
            "Veja: https://www.sorteionewstore.com.br/produto/1"
        ),
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "1",
                    "current_price": "199.90",
                    "url": "https://www.sorteionewstore.com.br/produto/1",
                }
            ]
        },
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "used_tray": True,
        },
    )

    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert report.valid is True
    assert report.checked_claims == 2


def test_invented_external_url_is_rejected():
    result = AgentResult(
        reply_text="Finalize em https://pagamento-inventado.example/pix",
        intent="commerce",
        commercial_data={"order_id": "123"},
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
        },
    )

    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert report.valid is False
    assert report.violations[0].kind == "url"


def test_official_pronta_entrega_catalog_url_is_accepted():
    result = AgentResult(
        reply_text=(
            "Os itens a pronta entrega estão em "
            "https://www.newstorerj.com/pronta-entrega"
        ),
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
        },
    )

    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert report.valid is True


def test_price_not_present_in_tool_facts_is_rejected():
    result = AgentResult(
        reply_text="O valor confirmado é R$ 299,90.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "current_price": "199.90"}],
        },
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "used_tray": True,
        },
    )

    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert report.valid is False
    assert report.violations[0].kind == "money"
    assert report.violations[0].claim == "299.90"


def test_order_identifier_must_match_verified_order():
    result = AgentResult(
        reply_text="O pedido XYZ999 foi criado.",
        intent="order",
        commercial_data={"order_id": "ABC123"},
        response_metadata={
            "domain": "commerce",
            "response_source": "tool",
            "used_tray": True,
        },
    )

    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert report.valid is False
    assert report.violations[0].kind == "order_id"


def test_shadow_mode_records_violation_without_changing_reply():
    original = "Pague em https://inventado.example/pix"
    result = AgentResult(
        reply_text=original,
        intent="order",
        commercial_data={"order_id": "123"},
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
        },
    )

    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="shadow",
    )

    assert validated.reply_text == original
    assert validated.response_metadata["factual_validation"]["valid"] is False
    assert (
        validated.response_metadata["factual_validation"]["fallback_applied"]
        is False
    )


def test_enforce_mode_uses_deterministic_factual_fallback():
    result = AgentResult(
        reply_text="Pague em https://inventado.example/pix",
        intent="order",
        commercial_data={"order_id": "123"},
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "factual_fallback_text": "Pedido criado. Consulte o pagamento no site oficial.",
        },
    )

    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="enforce",
    )

    assert (
        validated.reply_text
        == "Pedido criado. Consulte o pagamento no site oficial."
    )
    assert validated.safety_reason == "factual_validation_failed"
    assert validated.response_metadata["factual_validation"]["fallback_applied"]
    assert validated.response_metadata["factual_validation"]["fallback_required"]
    assert validated.response_metadata["factual_validation"]["risk_level"] == "high"


def test_enforce_default_fallback_is_customer_friendly():
    result = AgentResult(
        reply_text="Pague em https://inventado.example/pix",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "url": "https://evil.example/x"}]},
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="enforce",
    )
    assert validated.safety_reason == "factual_validation_failed"
    assert "Só mais um pouco" in validated.reply_text
    assert "encontrar exatamente qual relógio" in validated.reply_text


def test_derived_pix_from_list_price_is_grounded():
    """Shortlists print 15% Pix off list; enforce must not wipe the reply."""
    result = AgentResult(
        reply_text=(
            "Pela foto, parece Prospex Sea Samurai.\n"
            "1. Relógio Seiko Prospex Sea Samurai Automático Preto SRPL13K1\n"
            "A prazo: R$ 6.099,99\n"
            "À vista no Pix: R$ 5.184,99\n"
            "Link: https://www.newstorerj.com.br/relogios/seiko/srpl13k1"
        ),
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "11989",
                    "name": "Relógio Seiko Prospex Sea Samurai Automático Preto SRPL13K1",
                    "current_price": 6099.99,
                    "price": 6099.99,
                    "url": "https://www.newstorerj.com.br/relogios/seiko/srpl13k1",
                    "_revalidated": True,
                    "_factual_source": "tray_live",
                }
            ]
        },
        response_metadata={
            "domain": "commerce",
            "used_tray": True,
            "response_source": "image_vision",
        },
    )
    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="enforce",
    )
    assert report.valid is True
    assert report.fallback_required is False
    assert not any(
        claim.claim == "5184.99" for claim in report.unsupported_claims
    )

    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="enforce",
    )
    assert "5.184,99" in validated.reply_text
    assert validated.safety_reason != "factual_validation_failed"


def test_empty_catalog_customer_budget_is_not_a_sku_price():
    result = AgentResult(
        reply_text="Não encontrei relógios até R$ 2.500,00. Quer ajustar a faixa?",
        intent="commerce",
        safety_reason="recommendation_budget_miss",
        commercial_data={"products": []},
        response_metadata={
            "domain": "commerce",
            "used_tray": True,
            "hard_budget_max": 2500,
        },
    )
    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="enforce",
        commerce_state={"active_preferences": {"budget_max": 2500}},
    )
    assert report.valid is True
    assert any(
        claim.reason == "customer_budget_restated"
        for claim in report.supported_claims
    )


def test_honest_budget_miss_is_not_overwritten_in_enforce():
    result = AgentResult(
        reply_text="Não encontrei relógios até R$ 2.500,00.",
        intent="commerce",
        safety_reason="recommendation_budget_miss",
        commercial_data={"products": []},
        response_metadata={
            "domain": "commerce",
            "used_tray": True,
            "hard_budget_max": 2500,
            "factual_fallback_text": "Só mais um pouco, estou tentando encontrar exatamente qual relógio é esse.",
        },
    )
    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="enforce",
        commerce_state={"active_preferences": {"budget_max": 2500}},
    )
    assert "Não encontrei relógios" in validated.reply_text
    assert validated.safety_reason == "recommendation_budget_miss"


def test_promo_without_promotional_price_is_unsupported():
    result = AgentResult(
        reply_text="Temos promoção especial neste modelo.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "current_price": "199.90"}],
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    report = validate_factual_response(
        result,
        decision=_decision(result),
        mode="shadow",
    )
    assert report.valid is False
    assert report.violations[0].kind == "promo"
    assert report.unsupported_claims


def test_stock_conflict_and_payment_missing_evidence():
    stock_result = AgentResult(
        reply_text="O relógio está em estoque agora.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "stock": 0, "current_price": "10.00"}],
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    stock_report = validate_factual_response(
        stock_result,
        decision=_decision(stock_result),
        mode="shadow",
    )
    assert stock_report.valid is False
    assert any(item.kind == "stock" for item in stock_report.violations)
    assert stock_report.conflicting_claims

    paid_result = AgentResult(
        reply_text="Seu pedido já está pago.",
        intent="commerce",
        commercial_data={
            "order_id": "ABC123",
            "payment": {"status": "awaiting_payment"},
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    paid_report = validate_factual_response(
        paid_result,
        decision=_decision(paid_result),
        mode="shadow",
        commerce_state={"order_id": "ABC123", "order_payment_status": "awaiting_payment"},
    )
    assert paid_report.valid is False
    assert paid_report.risk_level == "critical"
    assert any(item.kind == "payment" for item in paid_report.violations)


def test_fact_evidence_is_attached_for_observability():
    result = AgentResult(
        reply_text="O produto custa R$ 10,00.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "current_price": "10.00"}],
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    validated = apply_factual_validation(
        result,
        decision=_decision(result),
        mode="shadow",
    )
    assert validated.response_metadata["fact_evidence"]
    assert validated.response_metadata["factual_validation"]["evidence_count"] >= 1
    assert "tray_adapter" in validated.response_metadata["factual_validation"][
        "evidence_sources"
    ]
