from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.catalog.retrieval.rerank import rerank_products
from app.catalog.retrieval.types import ProductRerankSelection
from app.models import SalesInterpretation
from app.ops.runtime_context import set_current_turn, reset_current_turn
from app.ops.turn_runtime import TurnRuntimeContext, LLMCallBudget


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls,expected_calls", [(2, 0), (4, 1)])
async def test_optional_rerank_preserves_final_response_slot(monkeypatch, max_calls, expected_calls):
    import app.catalog.retrieval.runtime as runtime
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(openai_api_key="offline", openai_model="offline"))
    context = TurnRuntimeContext(trace_id="offline", llm_budget=LLMCallBudget(max_calls=max_calls, enforce=True))
    async def mocked_parse(**kwargs):
        context.llm_budget.reserve(kwargs["call_type"])
        return SimpleNamespace(parsed=ProductRerankSelection(selected_product_ids=["2", "1"]))
    parse = AsyncMock(side_effect=mocked_parse)
    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", parse)
    context.llm_budget.reserve("decision")
    token = set_current_turn(context)
    try:
        interpretation = SalesInterpretation(domain="commerce", goal="find", needs_clarification=False,
                                             references_previous_context=False, confidence=0.99)
        result = await rerank_products([{"id": "1", "name": "Seiko"}, {"id": "2", "name": "Citizen"}], interpretation)
        assert {p["id"] for p in result} == {"1", "2"}
        assert parse.await_count == expected_calls
        context.llm_budget.reserve("response_composition")
    finally:
        reset_current_turn(token)
