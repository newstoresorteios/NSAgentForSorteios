from types import SimpleNamespace

from app.stories.instagram_story_models import StoryVisualUnderstanding, VisualProductRegion
from app.stories.instagram_story_service import _stored_vision_for_tray_retry
from app.stories.story_product_matcher import (
    product_scoped_analysis, tray_search_plan, tray_search_jobs, build_storefront_fallback_queries,
)


def scene():
    return StoryVisualUnderstanding(
        watch_count=1, visible_brands=["Sony", "Longines"],
        logo_hypotheses=["Longines", "Sony"],
        visible_text=["SONY", "LONGINES", "AUTOMATIC"],
        collection_hypotheses=["HydroConquest"], dial_colors=["azul"],
        product_regions=[VisualProductRegion(brand_hypothesis="Longines")],
    )


def test_watch_region_overrides_background_brand_without_mutating_input():
    original = scene()
    analysis = product_scoped_analysis(original)
    assert analysis.visible_brands == ["Longines"]
    assert "SONY" not in analysis.visible_text
    assert original.visible_brands == ["Sony", "Longines"]
    assert tray_search_plan(original)[0] == "Longines"


def test_old_saved_analysis_is_reinterpreted_before_retry():
    saved = SimpleNamespace(visual_analysis=scene().model_dump(), analysis_version="v2")
    analysis = _stored_vision_for_tray_retry(saved)
    assert analysis.visible_brands == ["Longines"]
    assert tray_search_plan(analysis)[0] == "Longines"


def test_every_search_job_uses_watch_brand_and_storefront_does_not_mix_brands():
    analysis = _stored_vision_for_tray_retry(SimpleNamespace(visual_analysis=scene().model_dump()))
    jobs = tray_search_jobs(analysis)
    assert jobs
    assert "Sony" not in str(jobs)
    queries = build_storefront_fallback_queries(
        analysis, brand=None, evidence_blob="", missing_line="",
    )
    assert queries
    assert all("sony" not in query.lower() for query in queries)
    assert any("longines" in query.lower() for query in queries)


def test_unresolved_scene_brands_do_not_pick_first_brand():
    analysis = scene().model_copy(update={"product_regions": []})
    assert tray_search_plan(analysis)[0] is None


def test_multi_watch_scene_does_not_promote_one_region_to_whole_scene():
    analysis = scene().model_copy(update={"multiple_products": True, "watch_count": 2})
    assert product_scoped_analysis(analysis).visible_brands == ["Sony", "Longines"]
    assert tray_search_plan(analysis)[0] is None
