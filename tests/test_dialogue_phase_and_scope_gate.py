"""P0 structural gates: dialogue phase + scope send-gate."""

from __future__ import annotations

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import AgentResult, SalesInterpretation
from app.persona_models import PersonaVersion
from app.persona_runtime import (
    build_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.sales.scope_send_gate import apply_scope_send_gate, validate_scope_send_gate


def _crono_chatbo_profile() -> dict:
    return {
        "name": "Crono New Store",
        "tone": "consultative",
        "qualification_rules": [
            "Você já tem um modelo em mente ou quer uma sugestão?",
            "Qual faixa de investimento você tem em mente?",
            "É para uso no dia a dia, trabalho, esporte ou uma ocasião especial?",
        ],
        "recommendation_rules": [
            "Recomendar somente peças que existem no catálogo integrado.",
        ],
    }


def _persona() -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "id": 20,
            "tenant_id": "newstore",
            "persona_key": "newstore_commercial",
            "version": 20,
            "name": "Crono",
            "instructions": "Eu sou o Crono.",
            "instructions_hash": "gate-tests",
            "status": "active",
            "metadata": {"chatboPersonaId": "11111111-1111-1111-1111-111111111111"},
        }
    )


@pytest.mark.offline_eval
def test_golden_felipe_order_context_no_budget_reask():
    """After order-status question, must not reopen budget qualification."""
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="inspect",
            subject={"product_type": "relógio"},
            preferences={},
            order_action="get_order_status",
            order_id="25522",
            information_needed=["catalog"],
            references_previous_context=True,
            needs_clarification=False,
            confidence=0.95,
        )
        commerce_state = CommerceConversationState(order_id="25522")
        discovery = sales_agent._discovery_state(
            interpretation,
            [],
            message_text="Como está meu pedido 25522?",
            commerce_state=commerce_state,
        )
        assert discovery["order_context_blocks_clarification"] is True
        assert discovery["persona_qualification_required"] is False

        plan = {"intent": "recommendation", "goal": "inspect"}
        assert sales_agent._needs_clarification_before_retrieval(
            interpretation,
            plan,
            discovery,
        ) is False

        question = sales_agent._persona_qualification_question(interpretation, discovery)
        assert question is None or "investimento" not in question.casefold()
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_ig_multi_brand_not_tissot_only():
    """Baltic + Hamilton request must not ship a Tissot-only shortlist."""
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"product_type": "relógio"},
        preferences={},
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    result = AgentResult(
        reply_text="Opções:\n1. Tissot Le Locle\n2. Tissot PRX",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "t1", "name": "Tissot Le Locle", "brand": "Tissot"},
                {"id": "t2", "name": "Tissot PRX", "brand": "Tissot"},
            ]
        },
        response_metadata={"presented_products": True, "domain": "commerce"},
    )
    report = validate_scope_send_gate(
        result,
        interpretation=interpretation,
        message_text="Tem Baltic ou Hamilton disponível?",
    )
    assert report.valid is False
    assert report.reason == "off_scope_brand_list"
    assert "Baltic" in report.requested_brands or "Hamilton" in report.requested_brands

    fixed, applied = apply_scope_send_gate(
        result,
        interpretation=interpretation,
        message_text="Tem Baltic ou Hamilton disponível?",
    )
    assert applied.valid is False
    assert fixed.safety_reason == "scope_send_gate_blocked"
    assert fixed.commercial_data is None or not fixed.commercial_data.get("products")
    assert "catálogo" in fixed.reply_text.casefold()


@pytest.mark.offline_eval
def test_dialogue_phase_blocks_qualify_on_shortlist():
    """Shortlist phase must not reopen persona qualification."""
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"brand": "Seiko", "product_type": "relógio"},
            preferences={},
            information_needed=["catalog"],
            references_previous_context=True,
            needs_clarification=True,
            confidence=0.8,
        )
        commerce_state = CommerceConversationState(
            active_domain="commerce",
            dialogue_phase="shortlist",
            last_presented_products=[
                {
                    "position": 1,
                    "product_id": "101",
                    "name": "Seiko 5",
                    "brand": "Seiko",
                },
            ],
        )
        discovery = sales_agent._discovery_state(
            interpretation,
            [],
            message_text="quero comprar",
            commerce_state=commerce_state,
        )
        assert discovery["dialogue_phase"] == "shortlist"
        assert discovery["persona_qualification_required"] is False
        assert discovery["force_retrieval"] is False
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_scope_send_gate_blocks_excluded_brand_list():
    """100% excluded-brand list must be blocked before send."""
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"product_type": "relógio"},
        preferences={
            "attributes": ["exclude_brand:Certina"],
        },
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    result = AgentResult(
        reply_text="Opções Certina",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "c1", "name": "Certina DS Action", "brand": "Certina"},
                {"id": "c2", "name": "Certina DS-7", "brand": "Certina"},
            ]
        },
        response_metadata={"presented_products": True, "domain": "commerce"},
    )
    report = validate_scope_send_gate(result, interpretation=interpretation)
    assert report.valid is False
    assert report.reason == "all_excluded_brand"

    fixed, _ = apply_scope_send_gate(result, interpretation=interpretation)
    assert fixed.safety_reason == "scope_send_gate_blocked"
    assert "catálogo" in fixed.reply_text.casefold()


def test_evolve_commerce_state_advances_dialogue_phase_to_shortlist():
    previous = CommerceConversationState(active_domain="commerce")
    result = AgentResult(
        reply_text="Opções",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "901", "name": "Opção A", "brand": "Seiko"},
            ]
        },
        response_metadata={"domain": "commerce", "presented_products": True},
    )
    updated = evolve_commerce_state(previous, result)
    assert updated.dialogue_phase == "shortlist"


def test_evolve_commerce_state_checkout_phase_on_cart():
    previous = CommerceConversationState(
        active_domain="commerce",
        dialogue_phase="buy",
        last_presented_products=[
            {"position": 1, "product_id": "101", "name": "Relógio", "brand": "Seiko"},
        ],
    )
    result = AgentResult(
        reply_text="Carrinho criado",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "cart_created",
            "cart_state": {
                "cart_session_id": "sess-1",
                "cart_url": "https://example.com/cart",
            },
        },
    )
    updated = evolve_commerce_state(previous, result)
    assert updated.dialogue_phase == "checkout"
