"""Case-size discovery, hard filter, and anti-loop (Ricardo 5511937118008)."""

from app.catalog_specs import (
    extract_case_size_range_from_text,
    interpretation_case_size_range,
    message_requests_other_brands,
    product_matches_case_size_range,
)
from app.models import SalesInterpretation
from app.preference_normalize import normalize_sales_interpretation
from app.product_retrieval import hard_filter_products
from app.sales.discovery import _discovery_state


def test_extract_case_size_range_variants():
    assert extract_case_size_range_from_text("Me mande opções entre 36 até 38mm") == (36, 38)
    assert extract_case_size_range_from_text("tamanhos de 36 a 38 mm") == (36, 38)
    assert extract_case_size_range_from_text("Gostei desse, 43/44mm") == (43, 44)
    assert extract_case_size_range_from_text("pulso pequeno") is None


def test_message_requests_other_brands():
    assert message_requests_other_brands("pode ser outras marcas") is True
    assert message_requests_other_brands("quero ver opções Tissot") is False
    assert message_requests_other_brands("outras opções de marca") is True
    assert message_requests_other_brands("não precisa ser certina") is True


def test_hard_filter_case_size_drops_out_of_range():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={"attributes": ["case_size:36-38mm"]},
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "big",
            "name": "Seiko Sumo",
            "brand": "Seiko",
            "case_size": "44",
            "current_price": 7599,
            "available": True,
        },
        {
            "id": "small",
            "name": "Seiko Alpinist",
            "brand": "Seiko",
            "case_size": "38",
            "current_price": 6999,
            "available": True,
        },
    ]
    filtered = hard_filter_products(products, interpretation, mode="recommendation")
    ids = {str(item["id"]) for item in filtered}
    assert ids == {"small"}


def test_discovery_force_retrieval_on_explicit_case_size():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={"budget_max": 8000, "budget_min": 5000, "style": "versátil"},
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=True,
        confidence=0.8,
    )
    state = _discovery_state(
        interpretation,
        [],
        message_text="Me mande opções com tamanhos entre 36 até 38mm",
    )
    assert state["force_retrieval"] is True
    assert state["case_size_range"] == (36, 38)


def test_discovery_affirmation_after_size_clarification():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={"budget_max": 8000, "style": "versátil", "attributes": ["case_size:36-38mm"]},
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=True,
        confidence=0.8,
    )
    recent = [
        {"role": "user", "content": "36 até 38mm"},
        {
            "role": "assistant",
            "content": "Quer que eu busque opções nessa faixa?",
            "metadata": {"safety_reason": "commerce_clarification"},
        },
    ]
    state = _discovery_state(interpretation, recent, message_text="Sim")
    assert state["force_retrieval"] is True


def test_normalize_unlocks_brands_and_inherits_case_size_on_sim():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={"budget_max": 8000},
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=True,
        confidence=0.8,
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="Sim",
        context_text="Me mande opções com tamanhos entre 36 até 38mm",
    )
    assert normalized.ready_for_retrieval is True
    assert normalized.stop_clarification is True
    assert interpretation_case_size_range(normalized) == (36, 38)

    brand_unlock = normalize_sales_interpretation(
        interpretation,
        message_text="pode ser outras marcas",
        context_text="",
    )
    assert brand_unlock.subject.brand is None
    assert "brand" in list(brand_unlock.preferences.explicit_no_preferences or [])


def test_product_matches_case_size_range():
    product = {"id": "1", "name": "Tissot PRX 39mm", "case_size": "39"}
    assert product_matches_case_size_range(product, 36, 38) is False
    product["case_size"] = "37"
    assert product_matches_case_size_range(product, 36, 38) is True
