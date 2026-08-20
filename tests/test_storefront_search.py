from app.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from app.storefront_search import parse_storefront_search_html
from app.story_match_decider import try_resolve_tied_candidates
from app.story_product_matcher import _candidate_core_listing_key, classify_match


def test_parse_storefront_search_html_reads_item_ids_and_rocks_name():
    html = r'''
    gtag('event', 'view_item_list', {"items":[
      {"item_id":"15494","item_name":"Rel\u00f3gio Christopher Ward C63 Sealander Rocks Autom\u00e1tico C63-36A3H1-S00A0-B1"},
      {"item_id":"14804","item_name":"Rel\u00f3gio Christopher Ward C63 Sealander Rocks Autom\u00e1tico C63-36A3H1-S00V0-B0"}
    ]});
    '''
    hits = parse_storefront_search_html(html)
    assert [hit["product_id"] for hit in hits] == ["15494", "14804"]
    assert "rocks" in hits[0]["name"].casefold()
    assert "C63-36A3H1-S00A0-B1" in hits[0]["reference"].upper()


def _cand(pid: str, listing: str) -> StoryProductCandidate:
    return StoryProductCandidate(
        catalog_item_key=f"product:{pid}",
        product_id=pid,
        score=1.0,
        match_reasons=["tray_brand_model:rocks", f"listing:{listing}"],
        source="tray_search",
    )


def test_core_listing_key_keeps_rocks_colorway():
    a0 = _cand(
        "15494",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00a0-b1",
    )
    v0 = _cand(
        "14804",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00v0-b0",
    )
    assert _candidate_core_listing_key(a0) != _candidate_core_listing_key(v0)


def test_classify_prefers_rocks_bracelet_b1_when_story_shows_bracelet():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        strap_types=["bracelet"],
        watch_count=1,
        readable_text_confidence=0.95,
    )
    a0_b1 = _cand(
        "15494",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00a0-b1",
    )
    v0_b0 = _cand(
        "14804",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00v0-b0",
    )
    winner = try_resolve_tied_candidates([v0_b0, a0_b1], analysis)
    assert winner is not None
    assert winner.product_id == "15494"
    status, top = classify_match([v0_b0, a0_b1], multiple_products=False, analysis=analysis)
    assert status == "matched"
    assert top is not None
    assert top.product_id == "15494"
