"""IQ-06: catalog index as primary candidate source."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog_index_primary import (
    fetch_primary_index_candidates,
    index_pool_is_sufficient,
    primary_index_min_count,
)
from app.models import ProductPreferences, ProductSubject, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    data = {
        "domain": "commerce",
        "goal": "recommend",
        "subject": ProductSubject(brand="Seiko", product_type="relógio"),
        "preferences": ProductPreferences(budget_max=5000),
        "references_previous_context": False,
        "enough_information_to_search": True,
        "ready_for_retrieval": True,
        "needs_clarification": False,
        "confidence": 0.9,
    }
    data.update(overrides)
    return SalesInterpretation(**data)


def test_primary_index_min_count_is_at_least_five():
    assert primary_index_min_count(candidate_limit=20) >= 5
    assert primary_index_min_count(candidate_limit=3) == 3


def test_index_pool_is_sufficient_threshold():
    products = [{"id": str(i)} for i in range(6)]
    assert index_pool_is_sufficient(products, candidate_limit=20) is True
    assert index_pool_is_sufficient(products[:2], candidate_limit=20) is False


def test_fetch_uses_constraints_before_lexical(monkeypatch):
    calls: list[str] = []

    class FakeRepo:
        def search_exact(self, **_kwargs):
            calls.append("exact")
            return []

        def search_by_constraints(self, **kwargs):
            calls.append("constraints")
            assert kwargs["brand"] == "Seiko"
            assert kwargs["max_price"] == 5000
            return [
                {
                    "product_id": "10",
                    "title_normalized": "Seiko 5 Sports",
                    "brand": "Seiko",
                    "price": 3200,
                    "catalog_item_key": "product:10",
                    "tenant_id": "newstore",
                }
            ]

        def search_lexical(self, **_kwargs):
            calls.append("lexical")
            return []

    monkeypatch.setattr(
        "app.catalog_index_repository.CatalogIndexRepository",
        FakeRepo,
    )
    monkeypatch.setattr(
        "app.catalog_index_primary.get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
        ),
    )

    products, strategy = fetch_primary_index_candidates(_interpretation())
    assert strategy == "constraints"
    assert [p["id"] for p in products] == ["10"]
    assert "lexical" not in calls
    assert calls == ["constraints"]


def test_fetch_passes_gender_constraint(monkeypatch):
    seen: dict = {}

    class FakeRepo:
        def search_exact(self, **_kwargs):
            return []

        def search_by_constraints(self, **kwargs):
            seen.update(kwargs)
            return [
                {
                    "product_id": "20",
                    "title_normalized": "Relógio Feminino Seiko",
                    "brand": "Seiko",
                    "gender": "feminino",
                    "catalog_item_key": "product:20",
                    "tenant_id": "newstore",
                }
            ]

        def search_lexical(self, **_kwargs):
            return []

    monkeypatch.setattr(
        "app.catalog_index_repository.CatalogIndexRepository",
        FakeRepo,
    )
    monkeypatch.setattr(
        "app.catalog_index_primary.get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
        ),
    )

    products, strategy = fetch_primary_index_candidates(
        _interpretation(
            preferences=ProductPreferences(
                budget_max=4000,
                recipient="esposa",
            )
        )
    )
    assert strategy == "constraints"
    assert seen.get("gender") == "feminino"
    assert products[0]["id"] == "20"


def test_fetch_falls_back_to_lexical(monkeypatch):
    class FakeRepo:
        def search_exact(self, **_kwargs):
            return []

        def search_by_constraints(self, **_kwargs):
            return []

        def search_lexical(self, **kwargs):
            assert "Seiko" in kwargs["query"]
            return [
                {
                    "product_id": "30",
                    "title_normalized": "Seiko Presage",
                    "brand": "Seiko",
                    "catalog_item_key": "product:30",
                    "tenant_id": "newstore",
                }
            ]

    monkeypatch.setattr(
        "app.catalog_index_repository.CatalogIndexRepository",
        FakeRepo,
    )
    monkeypatch.setattr(
        "app.catalog_index_primary.get_settings",
        lambda: SimpleNamespace(
            agent_catalog_index_read_enabled=True,
            agent_catalog_index_candidate_limit=30,
            agent_persona_tenant_id="newstore",
        ),
    )

    products, strategy = fetch_primary_index_candidates(
        _interpretation(preferences=ProductPreferences())
    )
    assert strategy == "lexical"
    assert products[0]["id"] == "30"


@pytest.mark.asyncio
async def test_recommendation_skips_tray_when_index_sufficient(monkeypatch):
    import app.sales_agent as sales_agent

    calls: list[tuple] = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"Seiko {arguments['product_id']}",
                "brand": "Seiko",
                "current_price": 2500 + int(arguments["product_id"]),
                "available": True,
                "available_in_store": True,
            }
        raise AssertionError(f"unexpected tray call: {name} {arguments}")

    index_products = [
        {
            "id": str(i),
            "product_id": str(i),
            "name": f"Seiko Modelo {i}",
            "brand": "Seiko",
            "price": 2000 + i * 100,
            "available": True,
            "available_in_store": True,
            "_from_catalog_index": True,
            "_factual_source": "catalog_index",
        }
        for i in range(1, 9)
    ]

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog_index_primary.fetch_primary_index_candidates",
        lambda *a, **k: (index_products, "constraints"),
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

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation()
    )

    search_calls = [c for c in calls if c[0] == "search_products"]
    assert search_calls == []
    assert result is not None
    assert len(result.commercial_data["products"]) == 3
    assert all(p["brand"] == "Seiko" for p in result.commercial_data["products"])


@pytest.mark.asyncio
async def test_recommendation_refreshes_tray_when_index_empty(monkeypatch):
    import app.sales_agent as sales_agent

    calls: list[tuple] = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"Relógio {arguments['product_id']}",
                "brand": "Seiko",
                "current_price": 3000,
                "available": True,
                "available_in_store": True,
            }
        return {
            "products": [
                {
                    "id": "1",
                    "name": "Seiko 5",
                    "brand": "Seiko",
                    "current_price": 3000,
                    "available": True,
                    "available_in_store": True,
                },
                {
                    "id": "2",
                    "name": "Seiko Presage",
                    "brand": "Seiko",
                    "current_price": 4500,
                    "available": True,
                    "available_in_store": True,
                },
                {
                    "id": "3",
                    "name": "Seiko Prospex",
                    "brand": "Seiko",
                    "current_price": 4800,
                    "available": True,
                    "available_in_store": True,
                },
            ]
        }

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.catalog_index_primary.fetch_primary_index_candidates",
        lambda *a, **k: ([], None),
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

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation()
    )

    search_calls = [c for c in calls if c[0] == "search_products"]
    assert search_calls
    assert result is not None
    assert len(result.commercial_data["products"]) >= 1
