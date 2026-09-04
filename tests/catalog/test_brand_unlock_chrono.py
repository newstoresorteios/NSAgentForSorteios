"""Brand unlock, chronograph force-retrieval, exclude-brand hard filter."""

from app.catalog.specs.catalog_specs import (
    extract_rejected_brands_from_text,
    message_requests_other_brands,
    message_wants_chronograph,
    product_matches_excluded_brand,
)
from app.models import SalesInterpretation
from app.catalog.specs.preference_normalize import normalize_sales_interpretation
from app.catalog.product_retrieval import hard_filter_products
from app.sales.discovery import _discovery_state


def test_joao_phrases_unlock_brand():
    assert message_requests_other_brands(
        "Não precisa ser da certina, quero outras opções de marca"
    )
    assert message_requests_other_brands("agora quero um crono, não precisa ser certina")
    assert message_requests_other_brands("Não quero chrono da certina")
    assert message_requests_other_brands("outras opções de marca")
    assert extract_rejected_brands_from_text("não precisa ser certina") == ["Certina"]
    assert extract_rejected_brands_from_text("Não quero chrono da certina") == ["Certina"]


def test_chronograph_detection():
    assert message_wants_chronograph("queria ver um crono tbm")
    assert message_wants_chronograph("quero um chronograph")
    assert not message_wants_chronograph("quero um diver")


def test_normalize_rejects_certina_and_forces_chrono():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Certina", "product_type": "relógio"},
        preferences={"budget_max": 10000, "style": "mergulho"},
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=True,
        confidence=0.8,
    )
    normalized = normalize_sales_interpretation(
        interpretation,
        message_text="agora quero um crono, não precisa ser certina",
        context_text="",
    )
    assert normalized.subject.brand is None
    assert normalized.ready_for_retrieval is True
    assert normalized.stop_clarification is True
    assert any(
        str(item).lower().startswith("exclude_brand:certina")
        for item in (normalized.preferences.attributes or [])
    )
    assert message_wants_chronograph(
        " ".join(str(item) for item in (normalized.preferences.attributes or []))
    ) or normalized.preferences.style == "cronógrafo"


def test_hard_filter_drops_excluded_brand():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"product_type": "relógio"},
        preferences={
            "budget_max": 10000,
            "attributes": ["exclude_brand:Certina", "cronógrafo"],
            "explicit_no_preferences": ["brand"],
        },
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "certina",
            "name": "Certina DS Action Chrono",
            "brand": "Certina",
            "current_price": 9499,
            "available": True,
        },
        {
            "id": "tissot",
            "name": "Tissot PR 100 Cronógrafo Preto",
            "brand": "Tissot",
            "current_price": 4299,
            "available": True,
        },
        {
            "id": "diver",
            "name": "Tissot Seastar Automatic",
            "brand": "Tissot",
            "current_price": 5000,
            "available": True,
        },
    ]
    filtered = hard_filter_products(products, interpretation, mode="recommendation")
    ids = {str(item["id"]) for item in filtered}
    assert "certina" not in ids
    assert "tissot" in ids
    assert "diver" not in ids  # chronograph hard filter drops non-chrono when chrono exists


def test_discovery_force_retrieval_on_chrono_and_brand_unlock():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"product_type": "relógio"},
        preferences={"budget_max": 10000},
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=True,
        confidence=0.8,
    )
    state = _discovery_state(
        interpretation,
        [],
        message_text="queria ver um crono tbm",
    )
    assert state["force_retrieval"] is True
    assert state["wants_chronograph"] is True

    unlocked = _discovery_state(
        interpretation,
        [],
        message_text="Não precisa ser da certina, quero outras opções de marca",
    )
    assert unlocked["force_retrieval"] is True
    assert unlocked["brand_unlock_requested"] is True


def test_product_matches_excluded_brand():
    product = {"id": "1", "brand": "Certina", "name": "DS Action"}
    assert product_matches_excluded_brand(product, ["Certina"]) is True
    assert product_matches_excluded_brand(product, ["Tissot"]) is False
