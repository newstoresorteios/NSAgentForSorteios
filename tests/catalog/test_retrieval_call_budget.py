from types import SimpleNamespace

import pytest

from app.catalog.retrieval.probes import run_probes
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.types import ProductRetrievalPlan, ProductRetrievalRequest
from app.models import SalesInterpretation


def _interpretation(*, mode: str) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="find" if mode == "exact" else "recommend",
        subject={"product_type": "relógio", "brand": "Citizen"},
        preferences={},
        information_needed=["catalog"],
        enough_information_to_search=True,
        ready_for_retrieval=True,
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.99,
    )


@pytest.mark.asyncio
async def test_exact_probes_prioritize_reference_and_stop_after_match(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.retrieval.runtime.get_settings",
        lambda: SimpleNamespace(agent_catalog_exact_search_call_limit=6),
    )
    calls = []

    async def tool(_name, arguments):
        calls.append(arguments)
        if arguments.get("reference") == "BN0151-09L":
            return {
                "products": [{
                    "id": "1",
                    "brand": "Citizen",
                    "reference": "BN0151-09L",
                    "name": "Relógio Citizen BN0151-09L",
                    "available": True,
                }]
            }
        return {"products": []}

    interpretation = _interpretation(mode="exact")
    interpretation.subject.reference = "BN0151-09L"
    requests = [
        ProductRetrievalRequest(strategy="token_and_search", tokens=("citizen",)),
        ProductRetrievalRequest(strategy="exact_reference", reference="BN0151-09L"),
        ProductRetrievalRequest(strategy="exact_query_full", query="Citizen Promaster"),
    ]
    session = RetrievalSession(
        interpretation=interpretation,
        retrieval_plan=ProductRetrievalPlan(mode="exact", requests=tuple(requests)),
        message_text="BN0151-09L",
        execute_tool=tool,
    )

    await run_probes(session, requests)

    assert calls[0]["reference"] == "BN0151-09L"
    assert len(calls) == 1
    assert [item["id"] for item in session.hard_filtered] == ["1"]


@pytest.mark.asyncio
async def test_probe_fanout_respects_per_retrieval_hard_limit(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.retrieval.runtime.get_settings",
        lambda: SimpleNamespace(agent_catalog_recommendation_search_call_limit=3),
    )
    calls = []

    async def tool(_name, arguments):
        calls.append(arguments)
        return {"products": []}

    requests = [
        ProductRetrievalRequest(strategy=f"fallback_{index}", name=str(index))
        for index in range(10)
    ]
    interpretation = _interpretation(mode="recommendation")
    interpretation.subject.brand = None
    session = RetrievalSession(
        interpretation=interpretation,
        retrieval_plan=ProductRetrievalPlan(
            mode="recommendation",
            requests=tuple(requests),
        ),
        message_text="até 2500",
        execute_tool=tool,
    )

    await run_probes(session, requests)

    assert len(calls) == 3
    assert session.search_call_count == 3
    assert session.search_budget_exhausted is True


@pytest.mark.asyncio
async def test_other_brands_keeps_searching_until_distinct_brands_are_found(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.retrieval.runtime.get_settings",
        lambda: SimpleNamespace(agent_catalog_recommendation_search_call_limit=6),
    )
    calls = []

    async def tool(_name, arguments):
        calls.append(arguments)
        if len(calls) <= 2:
            return {
                "products": [
                    {
                        "id": f"citizen-{len(calls)}-{index}",
                        "brand": "Citizen",
                        "name": "Relógio Citizen",
                        "available": True,
                    }
                    for index in range(3)
                ]
            }
        brands = ("Orient", "Casio")
        brand = brands[len(calls) - 3]
        return {
            "products": [{
                "id": brand.casefold(),
                "brand": brand,
                "name": f"Relógio {brand}",
                "available": True,
            }]
        }

    requests = [
        ProductRetrievalRequest(strategy=f"fallback_{index}", name=str(index))
        for index in range(6)
    ]
    interpretation = _interpretation(mode="recommendation")
    interpretation.subject.brand = None
    session = RetrievalSession(
        interpretation=interpretation,
        retrieval_plan=ProductRetrievalPlan(
            mode="recommendation",
            requests=tuple(requests),
        ),
        message_text="Quero outras opções, uma de cada marca",
        execute_tool=tool,
    )

    await run_probes(session, requests)

    assert len(calls) == 4
    assert {item["brand"] for item in session.hard_filtered} == {
        "Citizen",
        "Orient",
        "Casio",
    }
