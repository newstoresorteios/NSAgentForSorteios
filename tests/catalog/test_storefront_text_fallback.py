from unittest.mock import AsyncMock

import pytest

from app.catalog.media.storefront_search import StorefrontSearchResults
from app.catalog.retrieval.compiler import ProductRetrievalCompiler
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.storefront import (
    run_storefront_fallback,
    storefront_text_queries,
)
from app.models import SalesInterpretation


def _hamilton_murph_interpretation() -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "relógio",
            "brand": "Hamilton",
            "model": "Khaki Field Murph",
        },
        preferences={},
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=False,
        needs_clarification=False,
        confidence=0.97,
    )


def _session() -> RetrievalSession:
    interpretation = _hamilton_murph_interpretation()

    async def execute_tool(_tool, _arguments):
        raise AssertionError("The storefront candidate is collected before Tray revalidation")

    return RetrievalSession(
        interpretation=interpretation,
        retrieval_plan=ProductRetrievalCompiler.compile(interpretation),
        message_text="quero o hamilton khaki field murph",
        execute_tool=execute_tool,
    )


def test_storefront_query_comes_from_customer_text_and_operator_stopwords():
    queries = storefront_text_queries(_session())

    assert queries[0] == "hamilton khaki field murph"
    assert "quero" not in queries[0]
    assert queries[-1] == "Hamilton Khaki Field Murph"


@pytest.mark.asyncio
async def test_storefront_fallback_recovers_exact_watch_and_rejects_accessory(monkeypatch):
    session = _session()
    hits = StorefrontSearchResults(
        [
            {
                "product_id": "11821",
                "name": "Pulseira Hamilton Khaki Field Murph em couro",
                "url": "https://www.newstorerj.com.br/acessorios/pulseira-hamilton-murph",
                "brand": "Hamilton",
                "model": "Khaki Field Murph",
            },
            {
                "product_id": "16010",
                "name": (
                    "Rel�gio Hamilton Khaki Field Murph Autom�tico Azul "
                    "H70405740 38 mm"
                ),
                "url": (
                    "https://www.newstorerj.com.br/relogios/"
                    "relogio-hamilton-khaki-field-murph-automatico-azul-"
                    "h70405740-38-mm"
                ),
                "reference": "H70405740",
                "brand": "Hamilton",
                "model": "Khaki Field Murph",
            },
        ]
    )
    search = AsyncMock(return_value=hits)
    monkeypatch.setattr("app.catalog.retrieval.storefront.search_storefront", search)

    added = await run_storefront_fallback(session)

    assert added == 1
    assert [product["id"] for product in session.candidates] == ["16010"]
    assert [product["reference"] for product in session.hard_filtered] == ["H70405740"]
    assert session.candidates[0]["_factual_source"] == "storefront_search"
    search.assert_awaited_once_with(
        "hamilton khaki field murph",
        max_pages=3,
        limit=36,
    )
