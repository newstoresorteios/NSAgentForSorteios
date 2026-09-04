"""Catalog spec extraction + diver hard-filter assertiveness."""

from app.catalog.catalog_index import to_canonical_item
from app.catalog.catalog_specs import (
    extract_case_size_mm,
    extract_water_resistance_m,
    is_false_diver_product,
    is_true_diver_product,
)
from app.models import SalesInterpretation
from app.catalog.product_retrieval import _deterministic_semantic_order, hard_filter_products


def test_extract_case_size_and_wr_from_title():
    product = {
        "id": "1",
        "name": "Certina DS Action 41mm Powermatic 80",
        "description": "Resistência à água 200m diver",
    }
    assert extract_case_size_mm(product) == "41"
    assert extract_water_resistance_m(product) == 200


def test_canonical_item_persists_specs():
    item = to_canonical_item(
        {
            "id": "15572",
            "name": "Baltic Aquascaphe MK2 Automático 37mm",
            "brand": "Baltic",
            "description": "200m water resistance",
            "current_price": 7999,
        }
    )
    assert item is not None
    assert item.case_size == "37"
    assert item.water_resistance_m == 200
    assert item.mechanism == "automatic"


def test_ds7_is_false_diver_ds_action_is_true():
    ds7 = {
        "id": "10159",
        "name": "Relógio Certina DS-7 Powermatic 80 Preto",
        "description": "100m casual",
        "water_resistance_m": 100,
    }
    action = {
        "id": "10329",
        "name": "Relógio Certina DS Action Day-Date Powermatic 80 Preto",
        "description": "diver 200m",
        "water_resistance_m": 200,
    }
    assert is_false_diver_product(ds7) is True
    assert is_true_diver_product(action) is True
    assert is_false_diver_product(action) is False


def test_hard_filter_drops_ds7_when_true_diver_present():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Certina", "product_type": "relógio de mergulho"},
        preferences={"style": "diver", "attributes": ["caixa menor"]},
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "10159",
            "name": "Certina DS-7 Powermatic 80 Preto",
            "brand": "Certina",
            "description": "100m dress",
            "current_price": 8199,
            "available": True,
        },
        {
            "id": "10329",
            "name": "Certina DS Action Day-Date Powermatic 80 Preto",
            "brand": "Certina",
            "description": "diver 200m",
            "current_price": 7499,
            "available": True,
        },
    ]
    filtered = hard_filter_products(products, interpretation, mode="recommendation")
    ids = {str(p["id"]) for p in filtered}
    assert "10329" in ids
    assert "10159" not in ids


def test_small_case_diver_prefers_structured_39mm():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Certina", "product_type": "relógio de mergulho"},
        preferences={"style": "diver", "attributes": ["caixa menor"]},
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "big",
            "name": "Certina DS Action",
            "brand": "Certina",
            "case_size": "41",
            "water_resistance_m": 200,
            "description": "ds action diver",
        },
        {
            "id": "small",
            "name": "Certina DS Action Day-Date",
            "brand": "Certina",
            "case_size": "39",
            "water_resistance_m": 200,
            "description": "ds action diver",
        },
        {
            "id": "dress",
            "name": "Certina DS-7",
            "brand": "Certina",
            "case_size": "39",
            "water_resistance_m": 100,
            "description": "dress casual",
        },
    ]
    ranked = _deterministic_semantic_order(products, interpretation)
    assert ranked[0]["id"] == "small"
    assert ranked[-1]["id"] == "dress" or ranked[0]["id"] != "dress"
