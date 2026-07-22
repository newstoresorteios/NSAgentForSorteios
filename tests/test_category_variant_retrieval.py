from types import SimpleNamespace

import pytest

from app.category_resolver import CategoryResolver, CategorySelection, normalize_category_name
from app.models import IncomingMessage, SalesInterpretation
from app.product_retrieval import (
    ProductRetrievalCompiler,
    compact_candidates,
    enrich_product_variants,
    hard_filter_products,
    revalidate_products,
    semantic_preferences,
)


def _settings(api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(openai_api_key=api_key, openai_model="gpt-4.1-mini")


def _interpretation(
    *,
    goal: str = "recommend",
    product_type: str | None = "relógio",
    brand: str | None = None,
    model: str | None = None,
    preferences: dict | None = None,
    ready: bool = True,
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=goal,
        subject={"product_type": product_type, "brand": brand, "model": model},
        preferences=preferences or {},
        information_needed=["catalog"],
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=ready,
        stop_clarification=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.98,
    )


@pytest.mark.asyncio
async def test_category_resolver_matches_real_plural_category(monkeypatch):
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": [{"id": 10, "name": "Relógios"}]}
        return {"tree": {"id": 10, "name": "Relógios", "children": []}}

    monkeypatch.setattr("app.category_resolver.get_settings", lambda: _settings())
    resolution = await CategoryResolver(execute).resolve("relógio")

    assert normalize_category_name("Relógios") == normalize_category_name("relógio")
    assert resolution.selected_category_ids == ("10",)
    assert resolution.source == "normalized"
    assert calls[0] == ("list_categories", {"limit": 100, "page": 1})


@pytest.mark.asyncio
async def test_category_resolver_paginates_until_a_match(monkeypatch):
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories" and arguments["page"] == 1:
            return {"categories": [
                {"id": index, "name": f"Categoria {index}"}
                for index in range(100)
            ]}
        if name == "list_categories":
            return {"categories": [{"id": 200, "name": "Relógios"}]}
        return {"tree": {"id": 200, "name": "Relógios"}}

    monkeypatch.setattr("app.category_resolver.get_settings", lambda: _settings())
    resolution = await CategoryResolver(execute).resolve("relógio")

    assert resolution.selected_category_ids == ("200",)
    assert resolution.categories_loaded == 101
    assert [args["page"] for name, args in calls if name == "list_categories"] == [1, 2]


@pytest.mark.asyncio
async def test_category_descendants_are_limited_to_five_product_queries(monkeypatch):
    async def execute(name, arguments):
        if name == "list_categories":
            return {"categories": [{"id": 10, "name": "Relógios"}]}
        return {"tree": {
            "id": 10,
            "name": "Relógios",
            "children": [
                {"id": child_id, "name": f"Filha {child_id}"}
                for child_id in range(11, 21)
            ],
        }}

    monkeypatch.setattr("app.category_resolver.get_settings", lambda: _settings())
    resolution = await CategoryResolver(execute).resolve("relógio")

    assert len(resolution.product_category_ids) == 5
    assert resolution.product_category_ids[0] == "10"


def test_compiler_prioritizes_category_and_keeps_style_semantic():
    interpretation = _interpretation(preferences={"style": "social"})
    plan = ProductRetrievalCompiler.compile(interpretation, category_ids=("10",))

    assert plan.requests[0].strategy == "category"
    assert plan.requests[0].tool_arguments() == {
        "category_id": "10",
        "available": True,
        "available_in_store": True,
        "limit": 20,
        "page": 1,
    }
    assert plan.requests[1].strategy == "name_fallback"
    assert "social" not in plan.requests[0].tool_arguments().values()


@pytest.mark.asyncio
async def test_category_children_contribute_candidates_and_products_are_deduplicated(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    captured = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": [{"id": 10, "name": "Relógios"}]}
        if name == "get_category_tree":
            return {"tree": {"id": 10, "name": "Relógios", "children": [{"id": 11, "name": "Masculinos"}]}}
        if name == "search_products":
            if arguments.get("category_id") == "10":
                return {"products": [{"id": "A", "name": "A"}, {"id": "B", "name": "B"}]}
            if arguments.get("category_id") == "11":
                return {"products": [{"id": "B", "name": "B"}, {"id": "C", "name": "C"}]}
            return {"products": []}
        if name == "get_product":
            return {"id": arguments["product_id"], "name": arguments["product_id"], "current_price": 1000}
        raise AssertionError(name)

    async def rerank(products, interpretation):
        captured.extend(products)
        return products

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "rerank_products", rerank)
    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())

    product_requests = [args for name, args in calls if name == "search_products"]
    assert [request.get("category_id") for request in product_requests[:2]] == ["10", "11"]
    assert [product["id"] for product in captured] == ["A", "B", "C"]
    assert len(result.commercial_data["products"]) == 3


@pytest.mark.asyncio
async def test_candidate_pool_never_exceeds_twenty(monkeypatch):
    import app.sales_agent as sales_agent

    captured = []

    async def execute(name, arguments):
        if name == "list_categories":
            return {"categories": [{"id": 10, "name": "Relógios"}]}
        if name == "get_category_tree":
            return {"tree": {"id": 10, "name": "Relógios"}}
        if name == "search_products":
            return {"products": [{"id": str(index), "name": str(index)} for index in range(30)]}
        if name == "get_product":
            return {"id": arguments["product_id"], "name": arguments["product_id"], "current_price": 1000}
        raise AssertionError(name)

    async def rerank(products, interpretation):
        captured.extend(products)
        return products

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "rerank_products", rerank)
    await sales_agent._execute_compiled_product_retrieval(_interpretation())

    assert len(captured) == 20


def test_budget_filter_remains_objective_after_category_retrieval():
    products = [
        {"id": "A", "current_price": 9000},
        {"id": "B", "current_price": 11000},
    ]
    selected = hard_filter_products(
        products,
        _interpretation(preferences={"budget_max": 10000}),
        mode="recommendation",
    )
    assert [product["id"] for product in selected] == ["A"]


@pytest.mark.asyncio
async def test_catalog_request_retrieves_immediately_by_category(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": [{"id": 10, "name": "Relógios"}]}
        if name == "get_category_tree":
            return {"tree": {"id": 10, "name": "Relógios"}}
        if name == "search_products":
            return {"products": [{"id": "1", "name": "Citizen Tsuyosa", "current_price": 5000}]}
        if name == "get_product":
            return {"id": "1", "name": "Citizen Tsuyosa", "current_price": 5000}
        raise AssertionError(name)

    settings = _settings()
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("app.product_retrieval.get_settings", lambda: settings)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quais modelos vocês têm?"),
        {"primary_intent": "commerce"},
        {},
        _interpretation(ready=True),
        recent_turns=[],
    )

    first_product_request = next(args for name, args in calls if name == "search_products")
    assert first_product_request.get("category_id") == "10"
    assert result.safety_reason != "commerce_clarification"


def test_specific_product_keeps_exact_strategy_without_category():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(goal="find", product_type=None, brand="Tissot", model="Seastar")
    )
    assert plan.mode == "exact"
    assert plan.requests[0].strategy == "exact_brand_model"
    assert all(request.category_id is None for request in plan.requests)


def test_latest_interpretation_can_remove_previous_style():
    interpretation = _interpretation(
        preferences={"style": None, "explicit_no_preferences": ["style"]}
    )
    preferences = semantic_preferences(interpretation)
    assert "style" not in preferences
    assert preferences["explicit_no_preferences"] == ["style"]


@pytest.mark.asyncio
async def test_variant_color_is_loaded_as_real_evidence():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {
            "variants": [{
                "variant_id": "V1",
                "product_id": "P1",
                "color": "Preto",
                "stock": 2,
                "available_in_store": True,
            }]
        }

    enriched = await enrich_product_variants(
        [{"id": "P1", "name": "Modelo", "has_variation": True}],
        _interpretation(preferences={"color": "preto"}),
        execute,
    )

    assert calls == [("list_product_variants", {"product_id": "P1"})]
    assert enriched[0]["variants"][0]["color"] == "Preto"
    assert compact_candidates(enriched)[0]["variants"][0]["variant_id"] == "V1"


@pytest.mark.asyncio
async def test_category_selector_discards_invented_id(monkeypatch):
    import app.category_resolver as resolver_module

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=CategorySelection(selected_category_ids=["999"])
                    )
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    async def execute(name, arguments):
        if name == "list_categories":
            return {"categories": [
                {"id": 10, "name": "Relógios"},
                {"id": 11, "name": "Relógios"},
            ]}
        return {"tree": {}}

    monkeypatch.setattr(resolver_module, "get_settings", lambda: _settings("key"))
    monkeypatch.setattr(resolver_module, "AsyncOpenAI", FakeClient)
    resolution = await CategoryResolver(execute).resolve("relógio")

    assert resolution.selected_category_ids == ()
    assert resolution.source == "openai"


@pytest.mark.asyncio
async def test_category_api_failure_uses_name_fallback_without_false_catalog_empty(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"error": "unavailable"}
        if name == "search_products":
            return {"products": [{"id": "1", "name": "Relógio real", "current_price": 3000}]}
        if name == "get_product":
            return {"id": "1", "name": "Relógio real", "current_price": 3000}
        raise AssertionError(name)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())

    search = next(args for name, args in calls if name == "search_products")
    assert search.get("name") == "relógio"
    assert result.safety_reason != "category_lookup_failed"
    assert result.safety_reason != "recommendation_no_match"


@pytest.mark.asyncio
async def test_category_failure_and_empty_name_fallback_is_technical(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(name, arguments):
        if name == "list_categories":
            return {"error": "unavailable"}
        return {"products": []}

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())
    assert result.safety_reason == "category_lookup_failed"
    assert "não temos" not in result.reply_text.lower()


@pytest.mark.asyncio
async def test_top_three_are_revalidated_with_current_product_data():
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {
            "id": arguments["product_id"],
            "name": f"Atual {arguments['product_id']}",
            "current_price": 2000,
            "available": True,
            "available_in_store": True,
        }

    refreshed, failed = await revalidate_products(
        [{"id": str(index), "current_price": 1000} for index in range(5)],
        _interpretation(),
        execute,
    )

    assert failed is False
    assert [args["product_id"] for name, args in calls if name == "get_product"] == ["0", "1", "2"]
    assert all(product["current_price"] == 2000 for product in refreshed)
