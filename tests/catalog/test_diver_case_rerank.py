from app.models import SalesInterpretation
from app.catalog.product_retrieval import _deterministic_semantic_order


def test_diver_and_small_case_outranks_dress_100m():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Certina", "product_type": "relógio de mergulho"},
        preferences={
            "style": "diver",
            "attributes": ["caixa menor"],
            "budget_max": 10000,
        },
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "1",
            "name": "Relógio Certina DS-7 Powermatic 80 Preto 39mm",
            "brand": "Certina",
            "description": "100m casual dress",
        },
        {
            "id": "2",
            "name": "Relógio Certina DS Action Powermatic 80 Azul 41mm",
            "brand": "Certina",
            "description": "diver 200m mergulho",
        },
        {
            "id": "3",
            "name": "Relógio Certina DS Action Day-Date 39mm Preto",
            "brand": "Certina",
            "description": "diver 200m aquascaphe style",
        },
    ]
    ranked = _deterministic_semantic_order(products, interpretation)
    assert ranked[0]["id"] == "3"
    assert {p["id"] for p in ranked[:2]} == {"2", "3"}
    assert ranked[-1]["id"] == "1" or ranked[0]["id"] != "1"
