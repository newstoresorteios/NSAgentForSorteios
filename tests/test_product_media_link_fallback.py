import pytest

from app.commerce_context import CommerceProductReference
from app.product_media import resolve_presented_product_images, resolve_product_image


@pytest.mark.asyncio
async def test_image_failure_falls_back_to_product_url(monkeypatch):
    async def execute(name, arguments):
        assert name == "get_product"
        return {"error": "tray_adapter_unavailable", "status_code": 503}

    async def fake_ensure(product):
        patched = dict(product)
        patched["_product_url_dead"] = False
        return patched

    monkeypatch.setattr(
        "app.product_media.ensure_product_has_live_url",
        fake_ensure,
    )

    result = await resolve_product_image(
        product_reference=CommerceProductReference(
            product_id="1999",
            name="Seiko Monster",
            product_url="https://www.newstorerj.com.br/seiko-monster",
        ),
        execute=execute,
    )
    assert result.safety_reason == "product_media_link_fallback"
    assert "https://www.newstorerj.com.br/seiko-monster" in result.reply_text
    assert "João" not in result.reply_text


@pytest.mark.asyncio
async def test_listed_images_fallback_to_links_when_tray_down(monkeypatch):
    async def execute(name, arguments):
        return {"error": "tray_adapter_unavailable", "status_code": 503}

    async def fake_ensure(product):
        patched = dict(product)
        patched["_product_url_dead"] = False
        return patched

    monkeypatch.setattr(
        "app.product_media.ensure_product_has_live_url",
        fake_ensure,
    )

    result = await resolve_presented_product_images(
        product_references=[
            CommerceProductReference(
                product_id="1",
                name="Seiko A",
                product_url="https://www.newstorerj.com.br/a",
            ),
            CommerceProductReference(
                product_id="2",
                name="Seiko B",
                product_url="https://www.newstorerj.com.br/b",
            ),
        ],
        execute=execute,
    )
    assert result.safety_reason == "product_media_link_fallback"
    assert "https://www.newstorerj.com.br/a" in result.reply_text
    assert "https://www.newstorerj.com.br/b" in result.reply_text
    assert "Link com fotos" in result.reply_text


@pytest.mark.asyncio
async def test_dead_storefront_link_is_not_emitted(monkeypatch):
    async def execute(name, arguments):
        return {"error": "tray_adapter_unavailable", "status_code": 503}

    async def fake_ensure(product):
        patched = dict(product)
        patched["_product_url_dead"] = True
        return patched

    monkeypatch.setattr(
        "app.product_media.ensure_product_has_live_url",
        fake_ensure,
    )

    result = await resolve_product_image(
        product_reference=CommerceProductReference(
            product_id="96a288",
            name="Bulova 96A288",
            product_url=(
                "https://www.newstorerj.com.br/relogios-bulova/"
                "relogio-seminovo-bulova-marine-star-serie-c-automatico-preto-96a288"
            ),
        ),
        execute=execute,
    )
    assert result.safety_reason == "product_media_dead_link"
    assert "https://" not in (result.reply_text or "")
    assert "inconsistente" in (result.reply_text or "").casefold()
