"""Golden multi-turn sales backtests (offline, no live OpenAI/Tray).

Locks the Crono discovery → shortlist → selection contracts that ChatBo
persona rules must drive.
"""

from __future__ import annotations

import pytest

from app.commerce_context import CommerceConversationState, resolve_commerce_reference
from app.context_resume import merge_commerce_states
from app.models import SalesInterpretation
from app.persona_models import PersonaVersion
from app.persona_runtime import (
    build_persona_runtime,
    parse_recommendation_policy,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.product_retrieval import (
    apply_persona_presentation_order,
    customer_result_limit,
)


def _crono_chatbo_profile() -> dict:
    return {
        "name": "Crono New Store",
        "tone": "consultative",
        "greeting": "Olá! Eu sou o Crono, assistente virtual da New Store Relógios.",
        "qualification_rules": [
            "Você já tem um modelo em mente ou quer uma sugestão?",
            "Qual faixa de investimento você tem em mente?",
            "É para uso no dia a dia, trabalho, esporte ou uma ocasião especial?",
        ],
        "recommendation_rules": [
            "Recomendar somente peças que existem no catálogo integrado, sempre com o link oficial",
            "Se o cliente tem urgência, priorizar pronta entrega (2 a 5 dias úteis)",
            "Justificar toda recomendação com uma razão concreta ligada ao que o cliente disse",
            "Nunca apresentar mais de 3 peças de uma vez",
        ],
        "sales_goals": [
            "OBJETIVO — Fechar a venda dentro da conversa, enviando o link oficial do produto",
        ],
        "objection_handling": [
            "Se o cliente pedir desconto além do PIX, explicar a política e oferecer consultor humano",
        ],
        "examples": [
            "Cliente: quero um Seiko. Crono: pergunta faixa e estilo antes de listar.",
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
            "instructions": "Eu sou o Crono. 15% no PIX.",
            "instructions_hash": "golden",
            "status": "active",
            "metadata": {"chatboPersonaId": "11111111-1111-1111-1111-111111111111"},
        }
    )


@pytest.mark.offline_eval
def test_parse_recommendation_policy_from_chatbo_rules():
    policy = parse_recommendation_policy(
        [
            "Nunca apresentar mais de 3 peças de uma vez",
            "Se o cliente tem urgência, priorizar pronta entrega",
            "sempre com o link oficial",
            "Justificar toda recomendação",
        ]
    )
    assert policy["max_catalog_options"] == 3
    assert policy["prefer_ready_stock"] is True
    assert policy["require_official_catalog_link"] is True
    assert policy["justify_recommendations"] is True


@pytest.mark.offline_eval
def test_persona_runtime_makes_recommendation_rules_executable():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    assert runtime.max_catalog_options == 3
    assert runtime.prefer_ready_stock is True
    assert runtime.require_qualification_before_catalog is True
    assert len(runtime.qualification_prompts) >= 2
    skills = runtime.sales_skills_block()
    assert "Qualificação" in skills
    assert "Recomendação" in skills
    assert "Objeções" in skills
    assert "max_catalog_options: 3" in runtime.interpreter_policy_block()


@pytest.mark.offline_eval
def test_customer_result_limit_reads_persona_runtime():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile={
            **_crono_chatbo_profile(),
            "recommendation_rules": ["Nunca apresentar mais de 2 peças de uma vez"],
        },
    )
    token = set_persona_runtime(runtime)
    try:
        assert customer_result_limit() == 2
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_prefer_ready_stock_reorders_shortlist():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        products = [
            {"id": "1", "name": "Sob encomenda", "category_id": "10"},
            {"id": "2", "name": "Pronto", "category_id": "403"},
        ]
        ordered = apply_persona_presentation_order(products)
        assert [p["id"] for p in ordered] == ["2", "1"]
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_brand_only_seiko_requires_qualification():
    import app.sales_agent as sales_agent

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"product_type": "relógio", "brand": "Seiko"},
            preferences={},
            information_needed=["catalog"],
            references_previous_context=False,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.95,
        )
        state = sales_agent._discovery_state(interpretation, [])
        assert state["persona_qualification_required"] is True
        assert state["force_retrieval"] is False
        assert sales_agent._needs_clarification_before_retrieval(
            interpretation,
            {"intent": "product_search"},
            state,
        )
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_golden_list_position_one_binds_to_latest_seiko_not_stale_tissot():
    seiko_list = [
        {
            "position": 1,
            "product_id": "1429",
            "name": "Seiko King Samurai",
            "brand": "Seiko",
            "reference": "SRPF79K1",
        },
        {
            "position": 2,
            "product_id": "1945",
            "name": "Seiko Automático",
            "brand": "Seiko",
            "reference": "SRPB55K1",
        },
        {
            "position": 3,
            "product_id": "1949",
            "name": "Seiko King Turtle",
            "brand": "Seiko",
            "reference": "SRPE05K1",
        },
    ]
    latest = {
        "last_presented_products": seiko_list,
        "product_resolution_state": "options_presented",
        "active_product": None,
        "pending_action": None,
    }
    donor = {
        "cart_session_id": "old-cart",
        "pending_action": "choose_checkout_channel",
        "active_product": {
            "product_id": "641",
            "name": "Tissot Seastar",
            "brand": "Tissot",
        },
        "last_presented_products": [
            {
                "position": 1,
                "product_id": "641",
                "name": "Tissot",
                "brand": "Tissot",
            }
        ],
    }
    merged = merge_commerce_states(latest, donor)
    state = CommerceConversationState.model_validate(merged)
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="inspect",
        subject={},
        preferences={},
        information_needed=["catalog"],
        references_previous_context=True,
        reference_type="list_position",
        reference_position=1,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=False,
        needs_clarification=False,
        confidence=0.99,
    )
    ref, how = resolve_commerce_reference(interpretation, state)
    assert how == "product_id"
    assert ref is not None
    assert ref.product_id == "1429"
    assert "Seiko" in (ref.name or ref.brand or "")
