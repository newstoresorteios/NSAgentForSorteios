from types import SimpleNamespace

import pytest

from app.models import IncomingMessage, SalesInterpretation
from app.product_retrieval import (
    ProductRerankSelection,
    ProductRetrievalCompiler,
    hard_filter_products,
    rerank_products,
)


def _interpretation(
    *,
    goal: str = "recommend",
    product_type: str | None = "relógio",
    brand: str | None = None,
    model: str | None = None,
    reference: str | None = None,
    preferences: dict | None = None,
    ready: bool = True,
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=goal,
        subject={
            "product_type": product_type,
            "brand": brand,
            "model": model,
            "reference": reference,
        },
        preferences=preferences or {},
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=ready,
        stop_clarification=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.98,
    )


def test_compiler_uses_product_type_as_name_and_never_as_brand():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(preferences={"style": "social"})
    )

    assert plan.mode == "recommendation"
    assert plan.requests[0].name == "relógio"
    assert plan.requests[0].brand is None


def test_compiler_preserves_only_explicit_brand():
    plan = ProductRetrievalCompiler.compile(_interpretation(brand="Tissot"))

    assert plan.requests[0].name == "relógio"
    assert plan.requests[0].brand == "Tissot"
    assert all(request.brand != "relógio" for request in plan.requests)


def test_semantic_style_never_becomes_name_or_brand():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(preferences={"style": "esportivo"})
    )

    arguments = plan.requests[0].tool_arguments()
    assert arguments["name"] == "relógio"
    assert "brand" not in arguments
    assert "esportivo" not in arguments.values()


def test_budget_is_applied_after_retrieval_using_effective_price():
    products = [
        {"id": "A", "name": "A", "current_price": 3000},
        {"id": "B", "name": "B", "current_price": 5500},
        {"id": "C", "name": "C", "price": 4800, "promotional_price": 4500},
    ]

    selected = hard_filter_products(
        products,
        _interpretation(preferences={"budget_max": 5000}),
        mode="recommendation",
    )

    assert [product["id"] for product in selected] == ["A", "C"]


@pytest.mark.asyncio
async def test_candidate_pool_is_twenty_and_customer_result_is_three(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {
            "products": [
                {"id": str(index), "name": f"Relógio {index}", "current_price": 1000 + index}
                for index in range(20)
            ]
        }

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())

    assert calls == [("search_products", {"name": "relógio", "available": True, "limit": 20, "page": 1})]
    assert len(result.commercial_data["products"]) == 3


@pytest.mark.asyncio
async def test_reranker_discards_ids_outside_candidate_set(monkeypatch):
    import app.product_retrieval as retrieval

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=ProductRerankSelection(
                            selected_product_ids=["invented", "2"]
                        )
                    )
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        retrieval,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="key", openai_model="gpt-4.1-mini"),
    )
    monkeypatch.setattr(retrieval, "AsyncOpenAI", FakeClient)

    selected = await rerank_products(
        [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
        _interpretation(preferences={"style": "social"}),
    )

    assert [product["id"] for product in selected] == ["2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", [{}, {"style": "social"}])
async def test_ready_broad_request_retrieves_without_new_clarification(
    monkeypatch,
    preference,
):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {
            "products": [
                {"id": "1", "name": "Modelo Classic", "current_price": 3000},
                {"id": "2", "name": "Modelo Urban", "current_price": 4000},
            ]
        }

    settings = SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("app.product_retrieval.get_settings", lambda: settings)
    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="continuação comercial"),
        {"primary_intent": "commerce"},
        {},
        _interpretation(preferences=preference, ready=True),
        recent_turns=[],
    )

    assert calls[0][1]["name"] == "relógio"
    assert "brand" not in calls[0][1]
    assert result.safety_reason != "commerce_clarification"
    assert result.safety_reason != "product_not_found"
    assert len(result.commercial_data["products"]) == 2


@pytest.mark.asyncio
async def test_exact_missing_product_keeps_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def fake_execute(name, arguments):
        return {"products": []}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(
            goal="find",
            product_type=None,
            brand="Tissot",
            model="Seastar XYZ",
            ready=False,
        )
    )

    assert result.safety_reason == "product_not_found"
    assert "esse produto" in result.reply_text
