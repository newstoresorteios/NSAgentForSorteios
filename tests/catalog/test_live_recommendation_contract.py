from types import SimpleNamespace

import pytest

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.retrieval.present import present_compiled_results
from app.catalog.retrieval.session import RetrievalSession
from app.models import SalesInterpretation


def interpretation(**preferences):
    return SalesInterpretation(
        domain="commerce", goal="recommend", subject={"product_type": "relógio"},
        preferences=preferences, information_needed=["catalog"],
        enough_information_to_search=True, ready_for_retrieval=True,
        references_previous_context=False, needs_clarification=False, confidence=0.99,
    )


def product(pid, **changes):
    return {
        "id": str(pid), "name": f"Relógio {pid}", "brand": "Citizen",
        "price": 2000, "available": True, **changes,
    }


@pytest.mark.parametrize("preferences,rows,expected", [
    ({"style": "diver", "attributes": ["case_size:36-38mm"]},
     [product(1, name="Diver 200m", case_size="44"),
      product(2, name="Diver 200m", case_size="38")], ["2"]),
    ({"attributes": ["case_size:36-38mm", "chronograph"]},
     [product(1, name="Relógio", case_size="38"),
      product(2, name="Chronograph", case_size="38")], ["2"]),
    ({"style": "diver", "attributes": ["case_size:36-38mm", "chronograph"]},
     [product(1, name="Diver 200m Chronograph", case_size="44"),
      product(2, name="Diver 200m", case_size="38"),
      product(3, name="Diver 200m Chronograph", case_size="38")], ["3"]),
    ({"style": "diver", "attributes": ["case_size:36-38mm"]},
     [product(1, name="Diver 200m", case_size="44")], []),
])
def test_hard_filters_compose(preferences, rows, expected):
    found = hard_filter_products(rows, interpretation(**preferences), mode="recommendation")
    assert [row["id"] for row in found] == expected


@pytest.fixture
def presentation(monkeypatch):
    settings = SimpleNamespace(
        openai_api_key="", agent_catalog_index_read_enabled=False,
        agent_catalog_index_write_enabled=False, agent_revalidate_top_n=3,
        agent_rerank_selection_limit=15, agent_candidate_pool_limit=20,
    )
    monkeypatch.setattr("app.catalog.retrieval.runtime.get_settings", lambda: settings)
    monkeypatch.setattr("app.catalog.retrieval.present.customer_result_limit", lambda: 3)
    monkeypatch.setattr("app.catalog.index.catalog_index.hybrid_rank_products",
                        lambda rows, *args, **kwargs: rows)
    return settings


async def present(rows, updates, **preferences):
    calls = []
    async def tool(name, args):
        if name == "list_product_variants":
            return {"variants": []}
        assert name == "get_product"
        pid = args["product_id"]
        calls.append(pid)
        update = updates.get(pid, {})
        if "error" in update:
            return update
        return {**next(row for row in rows if row["id"] == pid), **update}
    session = RetrievalSession(
        interpretation=interpretation(budget_max=2500, **preferences),
        retrieval_plan=SimpleNamespace(mode="recommendation", candidate_limit=20),
        message_text="até 2500", execute_tool=tool, hard_filtered=rows,
        has_budget=True,
    )
    result = await present_compiled_results(session)
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"price": 3000}, {"available": False},
    {"ProductSettings": {"upon_request": True}},
    {"price": None},
])
async def test_live_change_removes_candidate_without_restoring_cache(presentation, change):
    result, calls = await present([product(1)], {"1": change})
    assert not (result.commercial_data or {}).get("products")
    assert result.response_metadata["product_resolution_state"] == "live_constraints_changed"
    assert result.response_metadata["presented_products"] is False
    assert calls == ["1"]


@pytest.mark.asyncio
async def test_live_price_change_backfills_fourth_ranked_candidate(presentation):
    result, calls = await present([product(i) for i in range(1, 5)], {"1": {"price": 3000}})
    assert [p["id"] for p in result.commercial_data["products"]] == ["2", "3", "4"]
    assert calls == ["1", "2", "3", "4"]
    assert all(p["_revalidated"] for p in result.commercial_data["products"])


@pytest.mark.asyncio
async def test_refill_preserves_brand_diversity(presentation):
    rows = [product(1), product(2, brand="Seiko"), product(3, brand="Orient"),
            product(4, brand="Seiko"), product(5, brand="Casio")]
    result, calls = await present(rows, {"1": {"available": False}})
    assert {p["brand"] for p in result.commercial_data["products"]} == {"Seiko", "Orient", "Casio"}
    assert calls == ["1", "2", "3", "5"]


@pytest.mark.asyncio
async def test_refill_stops_after_one_bounded_wave(presentation):
    rows = [product(i) for i in range(1, 11)]
    result, calls = await present(rows, {p["id"]: {"price": 3000} for p in rows})
    assert calls == ["1", "2", "3", "4", "5", "6"]
    assert not (result.commercial_data or {}).get("products")


@pytest.mark.asyncio
async def test_rate_limit_does_not_trigger_refill_or_restore_rejected_candidate(presentation):
    rows = [product(i) for i in range(1, 6)]
    result, calls = await present(rows, {
        "1": {"price": 3000}, "2": {"error": "rate_limited", "status_code": 429},
    })
    assert calls == ["1", "2"]
    assert not (result.commercial_data or {}).get("products")
    assert result.response_metadata["revalidation_failed"] is True


@pytest.mark.asyncio
async def test_partial_failure_keeps_only_confirmed_eligible_rows(presentation):
    rows = [product(i) for i in range(1, 6)]
    result, calls = await present(rows, {
        "1": {"price": 3000}, "3": {"error": "unavailable", "status_code": 503},
    })
    assert calls == ["1", "2", "3"]
    assert [p["id"] for p in result.commercial_data["products"]] == ["2"]


@pytest.mark.asyncio
async def test_live_case_size_is_checked_before_presenting(presentation):
    rows = [product(1, case_size="38"), product(2, case_size="38")]
    result, calls = await present(rows, {"1": {"case_size": "44"}},
                                  attributes=["case_size:36-38mm"])
    assert [p["id"] for p in result.commercial_data["products"]] == ["2"]
