"""Guided near-match shortlists for ambiguous / soft exact misses."""

from app.commerce.commerce_router import guided_near_match_result
from app.sales.tray_query_authority import (
    QueryAuthorization,
    bind_catalog_authorization,
    reset_catalog_authorization,
)
from tests.sales.test_tray_query_authority import OMEGA_OVER_BUDGET


def test_guided_near_match_lists_up_to_three_options():
    products = [
        {"id": "1", "name": "Bulova preto dourado 98B278", "brand": "Bulova"},
        {"id": "2", "name": "Bulova Classic preto", "brand": "Bulova"},
        {"id": "3", "name": "Bulova Marine Star preto", "brand": "Bulova"},
        {"id": "4", "name": "Bulova extra", "brand": "Bulova"},
    ]
    result = guided_near_match_result(products, brand="Bulova", limit=3)
    assert result.safety_reason == "exact_product_ambiguous_brand"
    assert result.response_metadata.get("guided_near_match") is True
    assert "É algum desses?" in result.reply_text
    assert "98B278" in result.reply_text
    presented = (result.commercial_data or {}).get("products") or []
    assert len(presented) == 3


def test_guided_near_match_empty_is_not_found():
    result = guided_near_match_result([], brand="Seiko")
    assert result.safety_reason == "product_not_found"


def test_guided_near_match_forbid_over_budget_is_honest_miss():
    from tests.sales.test_tray_query_authority import OMEGA_OVER_BUDGET
    from app.sales.tray_query_authority import (
        QueryAuthorization,
        bind_catalog_authorization,
        reset_catalog_authorization,
    )

    token = bind_catalog_authorization(
        QueryAuthorization(
            allowed=True,
            reason="test",
            tool="search_products",
            brand="Omega",
            budget_min=None,
            budget_max=5000,
            empty_outcome="honest_constraint_miss",
            forbid_near_match=True,
        )
    )
    try:
        result = guided_near_match_result(OMEGA_OVER_BUDGET, brand="Omega")
        assert result.safety_reason == "recommendation_budget_miss"
        assert "mais próximos" not in (result.reply_text or "")
        assert (result.commercial_data or {}).get("products") == []
    finally:
        reset_catalog_authorization(token)


def test_guided_near_match_forbid_in_budget_is_honest_list():
    from app.sales.tray_query_authority import (
        QueryAuthorization,
        bind_catalog_authorization,
        reset_catalog_authorization,
    )

    token = bind_catalog_authorization(
        QueryAuthorization(
            allowed=True,
            reason="test",
            tool="search_products",
            brand="Seiko",
            budget_min=None,
            budget_max=5000,
            empty_outcome="honest_constraint_miss",
            forbid_near_match=True,
        )
    )
    try:
        result = guided_near_match_result(
            [{"id": "1", "name": "Seiko 5 Sports", "brand": "Seiko", "price": 2190}],
            brand="Seiko",
        )
        assert (result.response_metadata or {}).get("guided_near_match") is not True
        assert "mais próximos" not in (result.reply_text or "")
        assert "Seiko 5 Sports" in (result.reply_text or "")
    finally:
        reset_catalog_authorization(token)
