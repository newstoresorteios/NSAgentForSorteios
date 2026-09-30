from unittest.mock import AsyncMock

import pytest

from app.stories.instagram_story_models import StoryVisualUnderstanding, StoryProductCandidate
from app.stories.story_product_matcher import tray_search_jobs, match_story_to_catalog, classify_match
from app.stories.story_match_decider import build_evidence_profile, score_catalog_overlap


def jota_analysis():
    return StoryVisualUnderstanding(
        visible_brands=["Longines"], visible_text=["LONGINES"],
        model_hypotheses=["HydroConquest GMT"], collection_hypotheses=["HydroConquest"],
        dial_colors=["azul"], strap_types=["borracha"], mechanisms_suggested=["GMT"],
        watch_count=1, readable_text_confidence=0.74, product_identity_confidence=0.69,
    )


def test_gmt_search_precedes_family_color_and_missing_gmt_is_conflict():
    analysis = jota_analysis()
    jobs = tray_search_jobs(analysis)
    assert "gmt" in [token.lower() for token in jobs[0][1]]
    candidate = StoryProductCandidate(catalog_item_key="product:15080", product_id="15080", score=1,
        match_reasons=["tray_brand_model:Longines HydroConquest azul",
                       "listing:Relógio Longines HydroConquest Automático Azul L3.782.4.96.9"])
    _, _, conflicts = score_catalog_overlap(candidate, build_evidence_profile(analysis))
    assert "missing_line:gmt" in conflicts
    assert classify_match([candidate], multiple_products=False, analysis=analysis)[0] == "ambiguous"


@pytest.mark.asyncio
async def test_gmt_candidates_survive_broad_color_results(monkeypatch):
    class EmptyRepo:
        def search_exact(self, **kwargs):
            return []
        def search_lexical(self, **kwargs):
            return []
    monkeypatch.setattr("app.catalog.index.repository.CatalogIndexRepository", EmptyRepo)
    monkeypatch.setattr("app.catalog.vision.product_image_index.visual_search_from_caption", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.catalog.index.catalog_index.index_products_best_effort", lambda *a, **kw: 0)
    monkeypatch.setattr("app.catalog.media.storefront_search.search_storefront", AsyncMock(return_value=[]))
    ordinary = {"id": "15080", "brand": "Longines", "name": "Longines HydroConquest Automático Azul L3.782.4.96.9"}
    gmt = {"id": "gmt-blue", "brand": "Longines", "name": "Longines HydroConquest GMT Azul"}
    async def search(name, args):
        assert name == "search_products"
        return {"products": [gmt] if "gmt" in [t.lower() for t in args["tokens"]] else [ordinary]}
    candidates = await match_story_to_catalog(tenant_id="test", analysis=jota_analysis(), execute_tool=search)
    assert candidates[0].product_id == "gmt-blue"
    for candidate in candidates:
        assert candidate.score_components.exact_identifier_score == 0
        if candidate.product_id == "15080":
            assert "missing_line:gmt" in candidate.mismatch_reasons
            assert candidate.score < candidates[0].score
