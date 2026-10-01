from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import IncomingMessage


@pytest.mark.asyncio
async def test_story_photo_can_identify_unavailable_internal_catalog_item(monkeypatch):
    from app.catalog.vision import catalog_evidence
    from app.stories import instagram_story_service

    indexed = {
        "id": "12918",
        "name": "Relógio Mido Baroncelli Heritage Automático Marfim M027.407.36.260.00",
        "model": "Baroncelli Heritage",
        "reference": "M027.407.36.260.00",
        "brand": "Mido",
        "available": False,
        "url": "https://loja.example/produto/12918",
        "_catalog_item_key": "product:12918",
    }
    hit = {
        "product_id": "12918",
        "image_url": "https://images.example/12918.jpg",
        "url": indexed["url"],
        "_index_product": indexed,
        "_image_color_error": 0.01,
    }
    monkeypatch.setattr(catalog_evidence, "policy", lambda key: {
        "imageStorefrontSearchEnabled": False,
        "imageStorefrontCandidateLimit": 30,
        "imageStorefrontMaxPages": 1,
        "imageStorefrontMaxQueries": 1,
        "imageStorefrontPerceptualDistanceMax": 12,
        "imageStorefrontPerceptualMinMargin": 3,
        "imageStorefrontColorErrorMax": 0.2,
        "imageStorefrontExactDistanceMax": 4,
        "imageStorefrontExactColorErrorMax": 0.05,
    }[key])
    monkeypatch.setattr(catalog_evidence, "_story_identity_hits", lambda *a, **k: ([hit], 0))
    monkeypatch.setattr(catalog_evidence, "rank_storefront_hits_by_image",
                        AsyncMock(return_value=[(2, hit)]))
    monkeypatch.setattr(instagram_story_service, "_revalidate_product",
                        AsyncMock(return_value=(None, True, "product_revalidation_failed")))
    identified = SimpleNamespace(
        brand="Mido", model="Baroncelli Heritage", color="marfim",
        model_dump=lambda **_: {"brand": "Mido", "model": "Baroncelli Heritage"},
    )
    incoming = IncomingMessage(channel="instagram", image_url="https://media.example/photo.jpg")
    result = await catalog_evidence.resolve_catalog_photo(
        incoming,
        identified,
        story_reference={
            "tenant_id": "shop",
            "story_media_id": "18118620119283493",
            "selected_region_index": 0,
        },
    )
    assert result.commercial_data["products"][0]["id"] == "12918"
    assert "indisponível" in result.reply_text
    assert result.response_metadata["response_source"] == "instagram_story_photo_evidence"
    assert result.response_metadata["last_story_product"]["match_status"] == "matched"


@pytest.mark.asyncio
async def test_story_photo_uses_strict_visual_review_when_pixel_distance_is_large(monkeypatch):
    from app.catalog.vision import catalog_evidence
    from app.stories import instagram_story_service

    indexed = {"id": "12918", "name": "Mido Baroncelli Heritage M027.407.36.260.00",
               "brand": "Mido", "available": False, "url": "https://loja.example/12918"}
    hit = {"product_id": "12918", "image_url": "https://images.example/12918.jpg",
           "_index_product": indexed, "_image_color_error": 0.4}
    values = {
        "imageStorefrontSearchEnabled": False, "imageStorefrontCandidateLimit": 30,
        "imageStorefrontMaxPages": 1, "imageStorefrontMaxQueries": 1,
        "imageStorefrontPerceptualDistanceMax": 12, "imageStorefrontPerceptualMinMargin": 3,
        "imageStorefrontColorErrorMax": 0.2, "imageStorefrontExactDistanceMax": 4,
        "imageStorefrontExactColorErrorMax": 0.05,
    }
    monkeypatch.setattr(catalog_evidence, "policy", lambda key: values[key])
    monkeypatch.setattr(catalog_evidence, "_story_identity_hits", lambda *a, **k: ([hit], 0))
    monkeypatch.setattr(catalog_evidence, "rank_storefront_hits_by_image",
                        AsyncMock(return_value=[(94, hit)]))
    monkeypatch.setattr(catalog_evidence, "_review_story_candidates_with_vision",
                        AsyncMock(return_value=hit))
    monkeypatch.setattr(instagram_story_service, "_revalidate_product",
                        AsyncMock(return_value=(None, True, "product_revalidation_failed")))
    identified = SimpleNamespace(brand="Mido", model="Baroncelli Heritage", color="marfim",
        model_dump=lambda **_: {"brand": "Mido", "model": "Baroncelli Heritage"})
    result = await catalog_evidence.resolve_catalog_photo(
        IncomingMessage(channel="instagram", image_url="https://media.example/photo.jpg"),
        identified,
        story_reference={"tenant_id": "shop", "story_media_id": "story"},
    )
    assert result.commercial_data["products"][0]["id"] == "12918"
    assert result.response_metadata["image_catalog_proof"]["source"] == "story_photo_visual_review"
