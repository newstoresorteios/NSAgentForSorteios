from app.catalog.retrieval.availability import select_diverse_brand_shortlist
from app.catalog.retrieval.hard_filter import (
    hard_filter_products,
    relax_soft_filters_for_empty_pool,
)
from app.models import SalesInterpretation


def _interp(**kwargs) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=kwargs.pop("goal", "recommend"),
        subject=kwargs.pop("subject", {"product_type": "relógio"}),
        preferences=kwargs.pop("preferences", {}),
        information_needed=["catalog"],
        references_previous_context=kwargs.pop("references_previous_context", False),
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
        **kwargs,
    )


def test_diverse_shortlist_picks_three_brands_when_unlocked():
    pool = [
        {"id": "1", "brand": "Seiko", "name": "Seiko 1"},
        {"id": "2", "brand": "Seiko", "name": "Seiko 2"},
        {"id": "3", "brand": "Tissot", "name": "Tissot 1"},
        {"id": "4", "brand": "Citizen", "name": "Citizen 1"},
        {"id": "5", "brand": "Orient", "name": "Orient 1"},
    ]
    selected = select_diverse_brand_shortlist(pool, _interp(), limit=3)
    brands = [item["brand"] for item in selected]
    assert len(selected) == 3
    assert len(set(brands)) == 3


def test_diverse_shortlist_keeps_rank_order_when_brand_locked():
    pool = [
        {"id": "1", "brand": "Seiko", "name": "Seiko 1"},
        {"id": "2", "brand": "Seiko", "name": "Seiko 2"},
        {"id": "3", "brand": "Tissot", "name": "Tissot 1"},
    ]
    selected = select_diverse_brand_shortlist(
        pool,
        _interp(subject={"product_type": "relógio", "brand": "Seiko"}),
        limit=3,
    )
    assert [item["id"] for item in selected] == ["1", "2", "3"]


def test_relax_soft_color_keeps_budget_when_pool_would_be_empty():
    products = [
        {
            "id": "1",
            "brand": "Seiko",
            "name": "Seiko preto",
            "price": 1800,
            "available": True,
        },
        {
            "id": "2",
            "brand": "Tissot",
            "name": "Tissot azul",
            "price": 2200,
            "available": True,
        },
        {
            "id": "3",
            "brand": "Citizen",
            "name": "Citizen caro",
            "price": 4800,
            "available": True,
        },
    ]
    interpretation = _interp(
        preferences={"color": "dourado", "style": "social", "budget_max": 2500}
    )
    hard = hard_filter_products(products, interpretation, mode="recommendation")
    # Recommendation color is already soft locally; budget still drops the 4800.
    assert {item["id"] for item in hard} == {"1", "2"}
    relaxed = relax_soft_filters_for_empty_pool(
        products,
        interpretation,
        mode="recommendation",
    )
    assert {item["id"] for item in relaxed} == {"1", "2"}
    assert all((item.get("price") or 0) <= 2500 for item in relaxed)
