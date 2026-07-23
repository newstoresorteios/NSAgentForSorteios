from types import SimpleNamespace

import pytest

from app.commerce_context import (
    CommerceConversationState,
    evolve_commerce_state,
    resolve_commerce_reference,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.product_retrieval import (
    ProductMatchError,
    ProductMatchSelection,
    match_specific_products,
    product_availability_state,
)


def _interpretation(
    *,
    brand: str | None,
    model: str,
    product_type: str | None = None,
    reference_type: str | None = None,
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": product_type,
            "brand": brand,
            "model": model,
        },
        preferences={},
        information_needed=["catalog"],
        references_previous_context=reference_type is not None,
        needs_clarification=False,
        reference_type=reference_type,
        confidence=0.99,
    )


def _settings(*, api_key: str = "key") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="gpt-4.1-mini",
    )


def _mock_matcher(
    monkeypatch,
    selected_ids: list[str],
    *,
    match_status: str | None = None,
    best_candidate_id: str | None = None,
):
    import app.product_retrieval as retrieval

    resolved_status = match_status or (
        "none" if not selected_ids
        else "exact" if len(selected_ids) == 1
        else "ambiguous"
    )
    resolved_best_id = (
        best_candidate_id
        if best_candidate_id is not None
        else selected_ids[0] if resolved_status == "exact" and selected_ids else None
    )

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=ProductMatchSelection(
                                match_status=resolved_status,
                                candidate_ids=selected_ids,
                                best_candidate_id=resolved_best_id,
                                confidence=0.9,
                            )
                        )
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings())
    monkeypatch.setattr(retrieval, "AsyncOpenAI", FakeClient)


@pytest.mark.asyncio
async def test_brand_candidates_resolve_partial_specific_model(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    candidates = [
        {"id": "1", "name": "Longines HydroConquest", "brand": "Longines"},
        {"id": "2", "name": "Longines Spirit Zulu Time", "brand": "Longines"},
        {"id": "3", "name": "Longines Conquest", "brand": "Longines"},
    ]

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if arguments == {"brand": "Longines", "limit": 20, "page": 1}:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            return {
                **candidates[1],
                "available": True,
                "available_in_store": True,
            }
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["2"])
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Zulu")
    )

    search_calls = [arguments for tool, arguments in calls if tool == "search_products"]
    assert search_calls[0] == {
        "name": "Zulu",
        "brand": "Longines",
        "limit": 20,
        "page": 1,
    }
    assert search_calls[1] == {
        "brand": "Longines",
        "limit": 20,
        "page": 1,
    }
    assert result.safety_reason != "product_not_found"
    assert [product["id"] for product in result.commercial_data["products"]] == ["2"]


@pytest.mark.asyncio
async def test_exact_structured_model_does_not_need_brand_fallback(monkeypatch):
    import app.sales_agent as sales_agent
    import app.product_retrieval as retrieval

    calls = []
    product = {
        "id": "10",
        "name": "Longines Spirit Zulu Time",
        "brand": "Longines",
        "model": "Spirit Zulu Time",
        "available": True,
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            return {"products": [product]}
        if tool == "get_product":
            return product
        raise AssertionError(tool)

    monkeypatch.setattr(
        retrieval,
        "AsyncOpenAI",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("exact model match must not need OpenAI matcher")
        ),
    )
    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings())
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Spirit Zulu Time")
    )

    assert [call for call in calls if call[0] == "search_products"] == [
        (
            "search_products",
            {
                "name": "Spirit Zulu Time",
                "brand": "Longines",
                "limit": 20,
                "page": 1,
            },
        )
    ]
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_exact_model_filters_unrelated_same_brand_candidates(monkeypatch):
    import app.product_retrieval as retrieval

    products = [
        {"id": "1", "name": "Tissot Seastar", "brand": "Tissot", "model": "Seastar"},
        {"id": "2", "name": "Tissot Tradition", "brand": "Tissot", "model": "Tradition"},
        {"id": "3", "name": "Tissot PRX", "brand": "Tissot", "model": "PRX"},
    ]
    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings(api_key=""))

    selected = await match_specific_products(
        products,
        _interpretation(brand="Tissot", model="Seastar"),
    )

    assert selected.status == "exact"
    assert [product["id"] for product in selected.products] == ["1"]


@pytest.mark.asyncio
async def test_brand_candidates_without_semantic_match_return_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("brand") and "name" not in arguments:
                return {
                    "products": [
                        {"id": "1", "name": "Longines Conquest", "brand": "Longines"},
                        {"id": "2", "name": "Longines Master Collection", "brand": "Longines"},
                    ]
                }
            return {"products": []}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, [])
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="ProdutoQueNaoExiste")
    )

    assert result.safety_reason == "product_not_found"


@pytest.mark.asyncio
async def test_found_but_unavailable_is_not_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    product = {
        "id": "20",
        "name": "Tissot Seastar",
        "brand": "Tissot",
        "model": "Seastar",
        "available": False,
        "available_in_store": False,
        "available_for_purchase": False,
    }

    async def execute(tool, arguments):
        if tool == "search_products":
            return {"products": [product]}
        if tool == "get_product":
            return product
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Tissot", model="Seastar")
    )

    assert result.safety_reason == "product_unavailable"
    assert result.response_metadata["product_resolution_state"] == "found_unavailable"
    assert result.commercial_data["availability_state"] == "unavailable"


@pytest.mark.asyncio
async def test_contextual_product_id_avoids_text_search(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "303",
                "name": "Modelo contextual",
                "available": True,
            }
        raise AssertionError(f"text search must not run: {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings(api_key=""))
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={
            "product_id": "303",
            "name": "Modelo contextual",
            "reference": "REF-303",
        },
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="continuação contextual"),
        {"primary_intent": "commerce"},
        {},
        _interpretation(
            brand=None,
            model="Modelo contextual",
            reference_type="current_product",
        ),
        commerce_state=state,
    )

    assert calls == [("get_product", {"product_id": "303"})]
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_product_matcher_discards_invented_id(monkeypatch):
    products = [
        {"id": "1", "name": "Modelo Alpha", "brand": "Marca"},
        {"id": "2", "name": "Modelo Beta", "brand": "Marca"},
    ]
    _mock_matcher(
        monkeypatch,
        ["invented", "2"],
        match_status="exact",
        best_candidate_id="2",
    )

    selected = await match_specific_products(
        products,
        _interpretation(brand="Marca", model="Beta aproximado"),
    )

    assert selected.status == "exact"
    assert [product["id"] for product in selected.products] == ["2"]


def test_availability_uses_commercial_flags_not_stock_alone():
    assert product_availability_state({
        "stock": 10,
        "available": False,
        "available_in_store": False,
        "available_for_purchase": False,
    }) == "unavailable"
    assert product_availability_state({
        "stock": 0,
        "ProductSettings": {"upon_request": True},
    }) == "available"


@pytest.mark.asyncio
async def test_matcher_technical_failure_is_not_reported_as_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "search_products":
            return {
                "products": [
                    {"id": "1", "name": "Candidato real", "brand": "Marca"}
                ]
            }
        raise AssertionError(tool)

    async def failed_matcher(products, interpretation):
        raise ProductMatchError("failed")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "match_specific_products", failed_matcher)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca", model="Modelo específico")
    )

    assert result.safety_reason == "product_match_failed"
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_partial_model_returns_plausible_matches_from_brand_candidates(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    candidates = [
        {"id": "1", "name": "Longines HydroConquest", "brand": "Longines"},
        {"id": "2", "name": "Longines Spirit Zulu Time 39", "brand": "Longines"},
        {"id": "3", "name": "Longines Spirit Zulu Time 42", "brand": "Longines"},
        {"id": "4", "name": "Longines Conquest", "brand": "Longines"},
    ]

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if arguments == {"brand": "Longines", "limit": 20, "page": 1}:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["2", "3"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Zulu")
    )

    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == ["2", "3"]
    assert result.response_metadata["product_resolution_state"] == "plausible_matches"
    assert result.response_metadata["presented_products"] is True
    assert "active_product" not in result.response_metadata
    assert any(
        tool == "search_products"
        and arguments == {"brand": "Longines", "limit": 20, "page": 1}
        for tool, arguments in calls
    )


@pytest.mark.asyncio
async def test_informal_product_name_can_return_ambiguous_real_candidates(monkeypatch):
    import app.sales_agent as sales_agent

    candidates = [
        {"id": "11", "name": "Citizen Aviation Alpha", "brand": "Citizen"},
        {"id": "12", "name": "Citizen Aviation Bravo", "brand": "Citizen"},
        {"id": "13", "name": "Citizen Classic", "brand": "Citizen"},
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("brand") == "Citizen" and "name" not in arguments:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["11", "12"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Citizen", model="Pilot")
    )

    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == ["11", "12"]


@pytest.mark.asyncio
async def test_unavailable_product_remains_in_ambiguous_identification(monkeypatch):
    import app.sales_agent as sales_agent

    candidates = [
        {"id": "21", "name": "Modelo parcial 39", "brand": "Marca"},
        {"id": "22", "name": "Modelo parcial 42", "brand": "Marca"},
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("brand") == "Marca" and "name" not in arguments:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            if product["id"] == "21":
                return {
                    **product,
                    "available": False,
                    "available_in_store": False,
                    "available_for_purchase": False,
                }
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["21", "22"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca", model="Modelo parcial")
    )

    assert [item["id"] for item in result.commercial_data["products"]] == ["21", "22"]
    assert result.commercial_data["products"][0]["availability_state"] == "unavailable"
    assert result.commercial_data["products"][1]["availability_state"] == "available"


def test_disambiguation_list_replaces_previous_list_and_resolves_choice():
    previous = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "old", "name": "Produto anterior"},
        last_presented_products=[
            {"position": 1, "product_id": "old", "name": "Lista anterior"},
        ],
    )
    result = AgentResult(
        reply_text="possibilidades",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "101", "name": "Possibilidade A"},
                {"id": "202", "name": "Possibilidade B"},
            ],
            "match_status": "ambiguous",
        },
        response_metadata={
            "domain": "commerce",
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
        },
    )

    updated = evolve_commerce_state(previous, result)
    choice = _interpretation(
        brand=None,
        model="",
        reference_type="list_position",
    ).model_copy(update={"reference_position": 2})
    resolved, resolved_by = resolve_commerce_reference(choice, updated)

    assert [item.product_id for item in updated.last_presented_products] == ["101", "202"]
    assert updated.active_product is None
    assert resolved.product_id == "202"
    assert resolved_by == "product_id"
