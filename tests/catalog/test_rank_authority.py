from app.catalog.index.catalog_index import hybrid_rank_products
from app.catalog.retrieval.rank_authority import (
    product_rank_ids,
    rank_catalog_products,
    shadow_compare_rank,
)
from app.models import SalesInterpretation
from app.sales.workflows.catalog_ranking import rank_candidates


def _interpretation() -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Omega", "product_type": "relógio"},
        preferences={"budget_max": 8000},
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )


def _products() -> list[dict]:
    return [
        {
            "id": "1",
            "name": "Omega Seamaster Diver 300M",
            "brand": "Omega",
            "current_price": 7200,
        },
        {
            "id": "2",
            "name": "Omega Speedmaster",
            "brand": "Omega",
            "current_price": 9500,
        },
    ]


def test_recommendation_authority_is_hybrid_rank():
    interpretation = _interpretation()
    products = _products()
    hybrid = hybrid_rank_products(products, interpretation, mode="recommendation")
    authority = rank_catalog_products(
        products, interpretation, mode="recommendation"
    )
    assert product_rank_ids(authority) == product_rank_ids(hybrid)[: len(authority)]
    assert authority[0]["id"] == "1"


def test_leftover_rank_candidates_is_shadow_only():
    interpretation = _interpretation()
    products = _products()
    live = rank_catalog_products(products, interpretation, mode="recommendation")
    leftover = rank_candidates(
        products,
        {"subject": {"brand": "Omega"}, "constraints": {"budget_max": 8000}},
    )
    agree = shadow_compare_rank(
        live_ids=product_rank_ids(live)[:1],
        other_ids=product_rank_ids(leftover)[:1],
        other_name="catalog_ranking.rank_candidates",
        mode="recommendation",
    )
    assert agree is True
    assert leftover[0]["id"] == live[0]["id"]


def test_shadow_compare_logs_disagreement(capsys):
    agree = shadow_compare_rank(
        live_ids=["1", "2"],
        other_ids=["2", "1"],
        other_name="score_catalog_candidates",
        mode="recommendation",
    )
    assert agree is False
    output = capsys.readouterr().out
    assert "[catalog.rank.shadow]" in output
    assert "'agree': False" in output
