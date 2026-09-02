from types import SimpleNamespace

import pytest

from app.factual_validator import validate_factual_response
from app.models import AgentResult, SalesInterpretation
from app.sales.tray_capability_contract import consult_tray_list_products_contract
from app.sales.tray_query_authority import (
    authorize_catalog_search,
    budget_hard_miss_result,
)
from app.agent_contracts import build_agent_decision
from app.models import IncomingMessage


def _interpretation(**kwargs) -> SalesInterpretation:
    subject = {
        "product_type": kwargs.pop("product_type", "relógio"),
        "brand": kwargs.pop("brand", "Omega"),
        "model": kwargs.pop("model", None),
        "reference": kwargs.pop("reference", None),
    }
    preferences = kwargs.pop("preferences", {"budget_max": 5000})
    return SalesInterpretation(
        domain="commerce",
        goal=kwargs.pop("goal", "recommend"),
        subject=subject,
        preferences=preferences,
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.98,
    )


OMEGA_OVER_BUDGET = [
    {
        "id": "10759",
        "product_id": "10759",
        "name": "Relógio Omega Seamaster Diver 300m Automático Preto",
        "brand": "Omega",
        "reference": "210.30.42.20.01.001",
        "price": 50299.99,
        "promotional_price": 42754.99,
        "available": True,
        "available_in_store": True,
    },
    {
        "id": "15270",
        "product_id": "15270",
        "name": "Relógio Omega Seamaster Aquaterra Automático Turquesa",
        "brand": "Omega",
        "reference": "220.10.41.21.03.006",
        "price": 56999.99,
        "promotional_price": 48449.99,
        "available": True,
        "available_in_store": True,
    },
]


def test_tray_contract_documents_price_gap_vs_adaptor():
    contract = consult_tray_list_products_contract()
    assert "brand" in contract.adapter_params
    assert "current_price_range" in contract.adapter_params
    assert "property_name" in contract.adapter_params
    assert "price" in contract.unmapped_price_params
    assert "price_range" in contract.unmapped_price_params
    assert contract.budget_enforcement == "tray_current_price_range_and_local_hard_filter"
    assert contract.docs_url.startswith("https://developers.tray.com.br")


def test_authorize_omega_budget_forbids_near_match():
    auth = authorize_catalog_search(_interpretation())
    assert auth.allowed is True
    assert auth.budget_max == 5000
    assert auth.brand == "Omega"
    assert auth.empty_outcome == "honest_constraint_miss"
    assert "price_range" in auth.contract["unmapped_price_params"]


def test_authorize_copies_forbid_near_match_flag():
    interp = _interpretation()
    interp._forbid_near_match = True
    auth = authorize_catalog_search(interp)
    assert auth.forbid_near_match is True
    assert auth.empty_outcome == "honest_constraint_miss"


def test_budget_miss_does_not_list_over_budget_omegas():
    result = budget_hard_miss_result(_interpretation(), OMEGA_OVER_BUDGET)
    assert result is not None
    assert result.safety_reason == "recommendation_budget_miss"
    assert "mais próximos" not in (result.reply_text or "")
    assert "5.000" in (result.reply_text or "")
    assert (result.commercial_data or {}).get("products") == []
    assert (result.commercial_data or {}).get("brand_floor_price") == 42754.99
    assert result.response_metadata.get("hard_budget_max") == 5000


def test_budget_miss_skipped_when_no_budget():
    result = budget_hard_miss_result(
        _interpretation(preferences={}),
        OMEGA_OVER_BUDGET,
    )
    assert result is None


def test_factual_validator_flags_presented_over_budget():
    result = AgentResult(
        reply_text="Estes Omega mais próximos batem com o que você pediu.",
        intent="commerce",
        commercial_data={"products": OMEGA_OVER_BUDGET},
        response_metadata={
            "domain": "commerce",
            "hard_budget_max": 5000,
            "used_tray": True,
        },
    )
    report = validate_factual_response(
        result,
        decision=build_agent_decision(
            IncomingMessage(channel="whatsapp", sender_key="whatsapp:test", text="omega"),
            result,
            openai_call_count=1,
        ),
        mode="enforce",
    )
    assert any(item.reason == "presented_over_budget" for item in report.violations)
    assert report.risk_level == "high"
    assert report.fallback_required is True


def test_factual_validator_flags_hermetique_when_mk2_locked():
    result = AgentResult(
        reply_text="O Hermétique cinza 37mm cabe no que você pediu.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "h1",
                    "name": "Relógio Baltic Hermétique Summer Automático Cinza",
                    "brand": "Baltic",
                    "price": 8900,
                }
            ]
        },
        response_metadata={
            "domain": "commerce",
            "used_tray": True,
            "active_preferences": {
                "locked_identity": {"brand": "Baltic", "model": "Aquascaphe mk2"}
            },
        },
    )
    report = validate_factual_response(
        result,
        decision=build_agent_decision(
            IncomingMessage(channel="whatsapp", sender_key="whatsapp:test", text="mk2 cinza"),
            result,
            openai_call_count=1,
        ),
        mode="enforce",
    )
    assert any(item.reason == "presented_model_mismatch" for item in report.violations)
    assert report.valid is False


@pytest.mark.asyncio
async def test_compiled_retrieval_does_not_near_match_omega_over_5k(monkeypatch):
    import app.sales_agent as sales_agent

    async def fake_execute(name, arguments):
        if name == "search_products":
            return {"products": OMEGA_OVER_BUDGET}
        if name in {"list_categories", "get_category_tree", "get_category"}:
            return {"categories": []}
        if name == "get_product":
            pid = str(arguments.get("product_id"))
            for product in OMEGA_OVER_BUDGET:
                if str(product["id"]) == pid:
                    return product
            return {"error": "not_found"}
        if name == "list_product_variants":
            return {"variants": []}
        return {}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog_index_primary.fetch_primary_index_candidates",
        lambda *a, **k: (list(OMEGA_OVER_BUDGET), "lexical"),
    )
    monkeypatch.setattr(
        "app.catalog_index.index_products_best_effort",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_write_enabled=False,
            agent_catalog_index_fallback_to_tray=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
            openai_api_key="",
            openai_model="gpt-4.1-mini",
        ),
    )

    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())
    assert result is not None
    assert result.safety_reason == "recommendation_budget_miss"
    assert "mais próximos" not in (result.reply_text or "")
    assert "Seamaster" not in (result.reply_text or "")
    presented = (result.commercial_data or {}).get("products") or []
    assert presented == []


@pytest.mark.asyncio
async def test_first_search_uses_contract_budget_when_interp_forgot_it(monkeypatch):
    import app.sales_agent as sales_agent
    from app.commerce_context import CommerceConversationState

    async def fake_execute(name, arguments):
        if name == "search_products":
            return {"products": OMEGA_OVER_BUDGET}
        if name in {"list_categories", "get_category_tree", "get_category"}:
            return {"categories": []}
        if name == "get_product":
            pid = str(arguments.get("product_id"))
            for product in OMEGA_OVER_BUDGET:
                if str(product["id"]) == pid:
                    return product
            return {"error": "not_found"}
        if name == "list_product_variants":
            return {"variants": []}
        return {}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog_index_primary.fetch_primary_index_candidates",
        lambda *a, **k: (list(OMEGA_OVER_BUDGET), "lexical"),
    )
    monkeypatch.setattr(
        "app.catalog_index.index_products_best_effort",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_write_enabled=False,
            agent_catalog_index_fallback_to_tray=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
            openai_api_key="",
            openai_model="gpt-4.1-mini",
        ),
    )

    interp = _interpretation(preferences={"budget_max": None, "occasion": "trabalho"})
    result = await sales_agent._execute_compiled_product_retrieval(
        interp,
        message_text="tem algum omega nessa faixa de preço?",
        commerce_state=CommerceConversationState(
            active_preferences={"budget": {"max": 5000}}
        ),
    )
    assert result is not None
    assert result.safety_reason == "recommendation_budget_miss"
    assert "mais próximos" not in (result.reply_text or "")
    assert (result.commercial_data or {}).get("products") == []
