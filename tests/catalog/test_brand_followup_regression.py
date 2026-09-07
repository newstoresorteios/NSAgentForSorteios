from types import SimpleNamespace

import pytest

from app.catalog.retrieval.compiler import ProductRetrievalCompiler
from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.types import ProductRetrievalPlan
from app.catalog.specs.catalog_specs import message_requests_other_brands
from app.models import AgentResult, SalesInterpretation
from app.sales.answer_council import apply_turn_contract_for_search
from app.sales.responder import deterministic_tray_copy_ready


FOLLOWUP = "Consegue me dar outras sugestões de marca, tipo um de cada marca nessa faixa de preço?"


def interpretation(**kwargs):
    return SalesInterpretation(
        domain="commerce", goal="recommend", information_needed=["catalog"],
        ready_for_retrieval=True, enough_information_to_search=True,
        references_previous_context=True, needs_clarification=False, confidence=0.95,
        **kwargs,
    )


@pytest.mark.parametrize("text", [FOLLOWUP, "outras sugestões de marca", "um de cada marca", "marcas diferentes"])
def test_brand_followup_keeps_saved_budget_and_drops_suggested_identity(text):
    state = SimpleNamespace(
        active_preferences={"budget_max": 2500},
        last_presented_products=[{"id": "1", "brand": "Citizen", "name": "Citizen Promaster"}],
    )
    bound = apply_turn_contract_for_search(
        interpretation(subject={"product_type": "relógio", "brand": "Citizen", "model": "Promaster"}),
        message_text=text, commerce_state=state,
    )
    assert bound.preferences.budget_max == 2500
    assert bound.subject.brand is None
    assert bound.subject.model is None
    assert ProductRetrievalCompiler.compile(bound).mode == "recommendation"
    products = [
        {"id": "2", "brand": "Orient", "name": "Relógio Orient", "price": 2200, "available": True},
        {"id": "3", "brand": "Seiko", "name": "Relógio Seiko", "price": 3000, "available": True},
    ]
    assert [p["id"] for p in hard_filter_products(products, bound, mode="recommendation")] == ["2"]


def test_exact_model_explicit_in_new_message_is_preserved():
    bound = apply_turn_contract_for_search(
        interpretation(subject={"product_type": "relógio", "model": "PRX"}),
        message_text="outras marcas para comparar com PRX",
    )
    assert bound.subject.model == "PRX"


@pytest.mark.asyncio
async def test_presentation_recovers_brands_discarded_by_semantic_top_n(monkeypatch):
    from app.catalog.retrieval import present, rerank, variants, runtime
    from app.catalog.index import catalog_index

    interp = interpretation(subject={"product_type": "relógio"}, preferences={"budget_max": 2500})
    pool = [
        {"id": str(i), "brand": brand, "name": f"Relógio {brand} {i}", "price": 2200, "available": True}
        for i, brand in enumerate(["Citizen"] * 25 + ["Orient", "Casio"], 1)
    ]
    session = RetrievalSession(
        interpretation=interp,
        retrieval_plan=ProductRetrievalPlan(mode="recommendation", requests=()),
        message_text=FOLLOWUP, execute_tool=None, has_budget=True,
    )
    session.hard_filtered = pool
    session.catalog_index_seeded = len(pool)
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(agent_catalog_index_write_enabled=False))
    monkeypatch.setattr(catalog_index, "hybrid_rank_products", lambda products, *a, **kw: products[:20])

    async def passthrough(products, *a, **kw):
        return products

    async def only_citizen(products, *a, **kw):
        return products[:3]

    async def revalidate(products, *a, **kw):
        return products, False

    monkeypatch.setattr(variants, "enrich_product_variants", passthrough)
    monkeypatch.setattr(present, "enrich_product_variants", passthrough)
    monkeypatch.setattr(rerank, "rerank_products", only_citizen)
    monkeypatch.setattr(present, "revalidate_products", revalidate)
    monkeypatch.setattr(present, "customer_result_limit", lambda: 3)
    result = await present.present_compiled_results(session)
    assert {p["brand"] for p in result.commercial_data["products"]} == {"Citizen", "Orient", "Casio"}


def test_recommendations_use_generative_composition():
    result = AgentResult(reply_text="Sim, encontrei", intent="commerce", handoff_required=False,
                         commercial_data={"products": [{"id": "1"}]})
    assert not deterministic_tray_copy_ready(result, {"intent": "recommendation"})
    assert not deterministic_tray_copy_ready(result, {"intent": "product_comparison"})
    assert message_requests_other_brands(FOLLOWUP)


def test_other_brands_prioritizes_new_brands_without_excluding_previous_options():
    from app.catalog.retrieval.availability import select_diverse_brand_shortlist

    interp = interpretation(subject={"product_type": "relógio"})
    interp._previously_presented_brands = ["Citizen"]
    pool = [{"id": str(i), "brand": brand} for i, brand in enumerate(
        ["Citizen", "Orient", "Casio", "Seiko"]
    )]
    selected = select_diverse_brand_shortlist(pool, interp, limit=3)
    assert [p["brand"] for p in selected] == ["Orient", "Casio", "Seiko"]
    assert select_diverse_brand_shortlist(pool[:1], interp, limit=3) == pool[:1]


def test_correction_turn_does_not_relock_previous_suggestions():
    state = SimpleNamespace(
        active_preferences={"budget_max": 2500, "explicit_no_preferences": ["brand"]},
        last_presented_products=[{"id": "1", "brand": "Citizen", "name": "Citizen Promaster"}],
    )
    bound = apply_turn_contract_for_search(
        interpretation(subject={"product_type": "relógio"}),
        message_text="mas já tinha encontrado", commerce_state=state,
    )
    assert bound.preferences.budget_max == 2500
    assert bound.subject.brand is None
    assert bound.subject.model is None
