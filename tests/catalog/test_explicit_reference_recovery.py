"""An explicit SKU must escape unresolved Story context and reach exact search."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.catalog.retrieval.compiler import ProductRetrievalCompiler
from app.catalog.retrieval.tokens import effective_product_reference, extract_reference_code
from app.models import IncomingMessage, SalesInterpretation
from app.tray.tray_tools import search_products


PRODUCT = {
    "id": "2243",
    "name": "Relógio Hamilton American Classic Boulton Mechanical H13519711",
    "reference": "H13519711",
    "ean": "758501659738",
    "brand": "hamilton",
    "model": "Amarican Classic",  # Actual catalog spelling differs from the request.
    "available": True,
}
REQUEST = "Hamilton american Classic boulton H13519711"


@pytest.mark.parametrize("reference", ["H13519711", "h13519711", "L47954782", "AB123456C"])
def test_long_compact_reference_is_recognized_in_customer_text(reference):
    assert extract_reference_code(f"Quero {reference}, qual valor?") == reference
    assert effective_product_reference(reference) == reference
    interpretation = SalesInterpretation(
        domain="commerce", goal="find", subject={"reference": reference},
        references_previous_context=False, needs_clarification=False, confidence=1,
    )
    plan = ProductRetrievalCompiler.compile(interpretation)
    assert plan.mode == "exact"
    assert plan.requests[0].tool_arguments()["reference"] == reference


@pytest.mark.parametrize("text", ["PH2000M", "C63", "SUB300", "4R35", "H50", "41mm", "200m", "R$ 13519711"])
def test_model_families_movements_and_measurements_are_not_inferred_as_references(text):
    assert extract_reference_code(text) is None


@pytest.mark.asyncio
async def test_customer_reference_bypasses_ambiguous_story_and_ready_delivery(monkeypatch):
    from app.agents.door_media import try_media_routes
    from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime

    token = set_persona_runtime(PersonaRuntimeConfig(
        loaded=True, enabled=True, tenant_id="newstore", workspace_id="shop",
    ))
    ref = {
        "tenant_id": "newstore", "workspace_id": "shop", "match_status": "ambiguous",
        "conversation_id": "thread", "sender_key": "customer",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "followup_terms": ["hamilton", "american", "classic", "boulton"],
        "catalog_query": "hamilton american classic boulton branco",
    }
    visual = AsyncMock(side_effect=AssertionError("Reference must reach normal catalog routing"))
    monkeypatch.setattr("app.sales.ready_delivery.enrich_story_ready_delivery", visual)
    monkeypatch.setattr("app.agents.door.handle_image_product_search", visual)
    message = IncomingMessage(text=REQUEST, channel="instagram", conversation_id="thread", sender_key="customer")
    try:
        assert await try_media_routes(message, SimpleNamespace(last_story_product=ref)) is None
        visual.assert_not_awaited()
    finally:
        reset_persona_runtime(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [{"reference": "H13519711"}, {"ean": "758501659738"}])
async def test_exact_identity_wins_over_conflicting_generated_search_descriptors(identity):
    client = SimpleNamespace(
        search_products=AsyncMock(return_value={"products": [PRODUCT]}),
        search_products_by_tokens=AsyncMock(side_effect=AssertionError("Must query exact identifier")),
    )
    result = await search_products(
        client, **identity, query=REQUEST, name="Hamilton American Classic Boulton",
        model="Boulton", brand="Hamilton", tokens=REQUEST.split(), limit=5, page=1,
    )
    assert [p["id"] for p in result["products"]] == ["2243"]
    client.search_products.assert_awaited_once_with(**identity, limit=5, page=1)
    client.search_products_by_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_lookup_preserves_commercial_constraints_and_rejects_other_skus():
    sibling = {**PRODUCT, "id": "other", "reference": "H13431553"}
    client = SimpleNamespace(search_products=AsyncMock(return_value={"products": [sibling, PRODUCT]}))
    result = await search_products(
        client, reference="H13519711", tokens=["Boulton"], current_price_range="0,9000",
        available=True, available_in_store=True, limit=5, page=1,
    )
    assert [p["id"] for p in result["products"]] == ["2243"]
    client.search_products.assert_awaited_once_with(
        reference="H13519711", current_price_range="0,9000", available=True,
        available_in_store=True, limit=5, page=1,
    )
    result = await search_products(client, reference="H13519711", exclude_product_ids=["2243"])
    assert result["products"] == []


@pytest.mark.asyncio
async def test_missing_exact_reference_does_not_fall_back_to_other_models():
    client = SimpleNamespace(search_products=AsyncMock(return_value={"products": []}))
    assert await search_products(client, reference="H13519711", query=REQUEST) == {"products": []}
    client.search_products.assert_awaited_once_with(reference="H13519711", limit=5)


@pytest.mark.asyncio
async def test_new_and_preowned_listings_with_same_reference_are_not_reported_missing():
    from app.catalog.retrieval.executor import _execute_compiled_product_retrieval_unlocked

    preowned = {**PRODUCT, "id": "15026", "name": "Relógio Seminovo Hamilton Boulton H13519711",
                "available": False, "available_in_store": False, "stock": 0}
    tool = AsyncMock(return_value={"products": [PRODUCT, preowned]})
    interpretation = SalesInterpretation(
        domain="commerce", goal="inspect", subject={"reference": "H13519711"},
        references_previous_context=True, needs_clarification=False, confidence=1,
    )
    result = await _execute_compiled_product_retrieval_unlocked(interpretation, execute_tool=tool)
    assert result.safety_reason == "ambiguous_product"
    assert result.response_metadata["product_resolution_state"] == "plausible_matches"
    assert "Não encontrei" not in result.reply_text
    assert "Seminovo" in result.reply_text and "indisponível" in result.reply_text
    assert [p["id"] for p in result.commercial_data["products"]] == ["2243", "15026"]
    assert result.response_metadata["clear_active_product"] is True
    tool.assert_awaited_once_with("search_products", {"reference": "H13519711", "limit": 5, "page": 1})
