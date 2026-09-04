from __future__ import annotations

import pytest

from app.catalog.storefront_search import _product_from_storefront_hit, hydrate_storefront_hits


@pytest.mark.asyncio
async def test_hydrate_storefront_hits_falls_back_when_get_product_fails():
    async def _fail_get_product(_tool: str, _args: dict):
        return {"error": "commerce_upstream_error"}

    hits = [
        {
            "product_id": "14742",
            "name": "Relógio Baltic Aquascaphe MK2 Automático Verde",
            "reference": "",
        }
    ]
    products = await hydrate_storefront_hits(hits, execute_tool=_fail_get_product)
    assert len(products) == 1
    assert products[0]["id"] == "14742"
    assert "mk2" in str(products[0]["name"]).casefold()
    assert products[0].get("storefront_only") is True


def test_product_from_storefront_hit_extracts_brand_and_sku():
    hit = {
        "product_id": "999",
        "name": "Relógio Bulova Breton Automático 96B332",
        "reference": "",
    }
    product = _product_from_storefront_hit(hit)
    assert product["brand"] == "Bulova"
    assert product["reference"] == "96B332"
