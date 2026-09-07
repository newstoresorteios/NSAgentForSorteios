import pytest

from app.catalog.retrieval.executor import execute_contextual_product_lookup
from app.commerce.commerce_context import CommerceProductReference
from app.models import SalesInterpretation


def _interpretation(**kwargs) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="inspect",
        information_needed=["catalog"],
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.98,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_get_product_404_is_product_not_found():
    async def tool(name, arguments):
        assert name == "get_product"
        return {"error": "commerce_upstream_error", "status_code": 404}

    result = await execute_contextual_product_lookup(
        _interpretation(),
        CommerceProductReference(product_id="999"),
        execute_tool=tool,
    )
    assert result.safety_reason == "product_not_found"
    assert result.safety_reason != "tray_adapter_unavailable"


@pytest.mark.asyncio
async def test_get_product_503_is_still_adapter_unavailable():
    async def tool(name, arguments):
        return {"error": "commerce_upstream_error", "status_code": 503}

    result = await execute_contextual_product_lookup(
        _interpretation(),
        CommerceProductReference(product_id="999"),
        execute_tool=tool,
    )
    assert result.safety_reason == "tray_adapter_unavailable"
