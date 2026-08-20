from __future__ import annotations

from app.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from app.storefront_search import parse_storefront_search_html
from app.story_match_decider import try_resolve_tied_candidates
from app.story_product_matcher import (
    _candidate_core_listing_key,
    build_storefront_fallback_queries,
    classify_match,
    tray_search_jobs,
)


def test_build_storefront_fallback_queries_for_baltic_mk2_verde():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Baltic"],
        visible_text=["BALTIC AQUASCAPHE MK2", "39 mm"],
        model_hypotheses=["Aquascaphe MK2"],
        dial_colors=["green"],
        watch_count=1,
    )
    evidence = "baltic aquascaphe mk2 green"
    queries = build_storefront_fallback_queries(
        analysis,
        brand="Baltic",
        evidence_blob=evidence,
        missing_line=["mk2"],
    )
    folded = [q.casefold() for q in queries]
    assert any("aquascaphe mk2 verde" in q for q in folded)
    assert any("baltic" in q and "mk2" in q for q in folded)


def test_build_storefront_fallback_queries_for_bulova_seminovo():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Bulova"],
        visible_text=[
            "Seminovo e limitado a apenas 350 unidades no mundo.",
            "BULOVA",
            "AUTOMATIC",
        ],
        dial_colors=[],
        watch_count=1,
    )
    evidence = "seminovo bulova automatic 350"
    queries = build_storefront_fallback_queries(
        analysis,
        brand="Bulova",
        evidence_blob=evidence,
        missing_line=[],
    )
    folded = [q.casefold() for q in queries]
    assert any("seminovo" in q and "automatico" in q for q in folded)
    assert any("350" in q for q in folded)


def test_tray_search_jobs_adds_seminovo_queries():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Bulova"],
        visible_text=["Seminovo e limitado a apenas 350 unidades no mundo.", "BULOVA"],
    )
    jobs = tray_search_jobs(analysis)
    assert any(
        (brand or "").casefold() == "bulova" and tokens == ["seminovo", "automatico"]
        for brand, tokens in jobs
    )


def test_parse_storefront_search_html_reads_baltic_mk2_verde_href():
    html = '''
    <a href="https://www.newstorerj.com.br/relogios-baltic/relogio-baltic-aquascaphe-mk2-automatico-verde">
    '''
    hits = parse_storefront_search_html(html)
    assert hits
    assert "mk2" in hits[0]["name"].casefold()


def test_classify_match_accepts_mk2_when_story_says_mk2():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Baltic"],
        visible_text=["BALTIC AQUASCAPHE MK2"],
        model_hypotheses=["Aquascaphe MK2"],
        dial_colors=["green"],
        watch_count=1,
        readable_text_confidence=0.95,
    )
    mk2 = StoryProductCandidate(
        catalog_item_key="product:14742",
        product_id="14742",
        score=1.0,
        match_reasons=[
            "tray_brand_model:Baltic aquascaphe mk2 verde",
            "listing:relogio baltic aquascaphe mk2 automatico verde",
        ],
        source="tray_search",
    )
    status, top = classify_match([mk2], multiple_products=False, analysis=analysis)
    assert status == "matched"
    assert top is not None
    assert top.product_id == "14742"
