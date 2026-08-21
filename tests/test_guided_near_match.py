"""Guided near-match shortlists for ambiguous / soft exact misses."""

from app.commerce_router import guided_near_match_result


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
