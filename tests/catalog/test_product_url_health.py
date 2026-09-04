from __future__ import annotations

import pytest

from app.catalog.media.product_media import (
    resolve_live_product_url,
    storefront_url_candidates,
)


def test_storefront_url_candidates_include_spelling_and_bracelet_suffixes():
    url = (
        "https://www.newstorerj.com.br/relogios/relogios-christopher-ward/"
        "relogio-christopher-ward-c63-seander-automatico-verde-c63-39ada3-s00v1-vc"
    )
    variants = storefront_url_candidates(url)
    joined = "\n".join(variants)
    assert "sealander" in joined
    assert joined.count("-vk") >= 1
    assert "/christopher-ward/" in joined


@pytest.mark.asyncio
async def test_resolve_live_product_url_skips_example_hosts():
    url = "https://loja.example/produto/abc"
    assert await resolve_live_product_url(url) == url
