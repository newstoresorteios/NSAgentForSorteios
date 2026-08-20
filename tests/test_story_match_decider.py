"""Consensus decider for Instagram Story catalog matching."""

from __future__ import annotations

from app.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from app.story_match_decider import (
    build_evidence_profile,
    rerank_candidates_with_consensus,
    try_resolve_tied_candidates,
)
from app.story_product_matcher import classify_match, tray_search_jobs


def _fold(value: str) -> str:
    return value.casefold()


def _candidate(product_id: str, listing: str, *, score: float = 1.0) -> StoryProductCandidate:
    return StoryProductCandidate(
        catalog_item_key=f"product:{product_id}",
        product_id=product_id,
        score=score,
        match_reasons=[f"tray_brand_model:Christopher Ward C63", f"listing:{listing}"],
        source="tray_search",
    )


def test_evidence_profile_reads_title_block_from_visible_text():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=[
            "CHRISTOPHER WARD",
            "C63 SEALANDER ROCKS",
            "36 mm",
            "Automático",
        ],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        collection_hypotheses=["C63 Sealander Rocks"],
        visible_references=["C63 SEALANDER ROCKS"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
    )
    profile = build_evidence_profile(analysis)
    assert "rocks" in profile.positive_tokens
    assert "sealander" in profile.positive_tokens
    assert profile.size_mm == "36"
    assert profile.text_path_score >= 0.9


def test_tray_search_jobs_use_model_line_not_brand_only():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=[
            "CHRISTOPHER WARD",
            "C63 SEALANDER ROCKS",
            "36 mm",
            "Automático",
        ],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        collection_hypotheses=["C63 Sealander Rocks"],
        visible_references=["C63 SEALANDER ROCKS"],
        dial_colors=["green"],
    )
    jobs = tray_search_jobs(analysis)
    assert jobs
    first_brand, first_tokens = jobs[0]
    assert first_brand == "Christopher Ward"
    folded = {t.casefold() for t in first_tokens}
    assert "c63" in folded
    assert "sealander" in folded or "c63" in folded
    assert any("rocks" in {t.casefold() for t in tokens} for _brand, tokens in jobs)
    assert any(
        {t.casefold() for t in tokens} == {"sealander", "verde"}
        or {t.casefold() for t in tokens} == {"c63", "sealander"}
        for _brand, tokens in jobs
    )
    assert not any(
        job[1] == ["CHRISTOPHER", "WARD"] or _fold(t) == "ward" and len(job[1]) <= 2
        for job in jobs
        for t in job[1]
    )


def test_decider_rejects_twelve_when_story_says_c63():
    from app.story_match_decider import rerank_candidates_with_consensus

    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
    )
    twelve = _candidate(
        "11167",
        "relogio christopher ward the twelve automatico titanio verde c12-36ahc1-t00v0-b0",
    )
    seander = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    ordered = rerank_candidates_with_consensus([twelve, seander], analysis)
    assert ordered[0].product_id == "10611"


def test_decider_prefers_non_gmt_when_story_says_sealander_rocks():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        collection_hypotheses=["C63 Sealander Rocks"],
        visible_references=["C63 SEALANDER ROCKS"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
    )
    seander = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    gmt = _candidate(
        "10697",
        "relogio christopher ward c63 sealander gmt automatico verde c63-39agm3-s00v1-b0",
    )
    ordered = rerank_candidates_with_consensus([gmt, seander], analysis)
    assert ordered[0].product_id == "10611"
    winner = try_resolve_tied_candidates([seander, gmt], analysis)
    assert winner is None


def test_decider_prefers_rocks_36_over_similar_seander_39():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        collection_hypotheses=["C63 Sealander Rocks"],
        visible_references=["C63 SEALANDER ROCKS"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
        watch_count=1,
    )
    seander_39 = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    rocks_36 = _candidate(
        "rocks36",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00a0-b1",
    )
    ordered = rerank_candidates_with_consensus([seander_39, rocks_36], analysis)
    assert ordered[0].product_id == "rocks36"
    status, top = classify_match(
        [seander_39, rocks_36],
        multiple_products=False,
        analysis=analysis,
    )
    assert status == "matched"
    assert top is not None
    assert top.product_id == "rocks36"


def test_classify_match_does_not_quote_39mm_when_story_is_rocks_36():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
        watch_count=1,
    )
    seander = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    gmt = _candidate(
        "10697",
        "relogio christopher ward c63 sealander gmt automatico verde c63-39agm3-s00v1-b0",
    )
    status, top = classify_match([gmt, seander], multiple_products=False, analysis=analysis)
    assert status != "matched"


def test_classify_match_resolves_cw_tie_with_analysis():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        readable_text_confidence=0.95,
        watch_count=1,
    )
    rocks = _candidate(
        "rocks36",
        "relogio christopher ward c63 sealander rocks automatico c63-36a3h1-s00a0-b1",
    )
    seander = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    gmt = _candidate(
        "10697",
        "relogio christopher ward c63 sealander gmt automatico verde c63-39agm3-s00v1-b0",
    )
    status, top = classify_match(
        [gmt, seander, rocks], multiple_products=False, analysis=analysis
    )
    assert status == "matched"
    assert top is not None
    assert top.product_id == "rocks36"


def test_tray_search_jobs_use_ballade_and_color_not_powermatic_80():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Tissot"],
        visible_text=["TISSOT BALLADE", "39 mm", "Motor: Powermatic 80"],
        model_hypotheses=["Tissot Ballade Powermatic 80"],
        collection_hypotheses=["Tissot Ballade"],
        dial_colors=["azul claro"],
    )
    jobs = tray_search_jobs(analysis)
    token_sets = [{t.casefold() for t in tokens} for _brand, tokens in jobs]
    assert any("ballade" in tokens and "azul" in tokens for tokens in token_sets)
    assert not any("claro" in tokens for tokens in token_sets)
    assert not any("80" in tokens for tokens in token_sets)
    assert not any("powermatic" in tokens for tokens in token_sets)


def test_classify_match_rejects_39mm_seander_when_story_is_36mm_rocks():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        watch_count=1,
        readable_text_confidence=0.95,
    )
    seander = _candidate(
        "10611",
        "relogio christopher ward c63 seander automatico verde c63-39ada3-s00v1-vc",
    )
    status, _top = classify_match([seander], multiple_products=False, analysis=analysis)
    assert status != "matched"


def test_classify_match_rejects_all_gmt_pool_when_story_omits_gmt():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Christopher Ward"],
        visible_text=["CHRISTOPHER WARD", "C63 SEALANDER ROCKS", "36 mm"],
        model_hypotheses=["C63 Sealander Rocks 36mm"],
        dial_colors=["green"],
        watch_count=1,
        readable_text_confidence=0.95,
    )
    gmt_a = _candidate(
        "10697",
        "relogio christopher ward c63 sealander gmt automatico verde",
    )
    gmt_b = _candidate(
        "10727",
        "relogio christopher ward c63 sealander gmt automatico verde hko",
    )
    status, _top = classify_match([gmt_a, gmt_b], multiple_products=False, analysis=analysis)
    assert status != "matched"


def test_classify_match_rejects_gmt_when_story_says_mk2():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Baltic"],
        visible_text=["BALTIC AQUASCAPHE MK2"],
        model_hypotheses=["Aquascaphe MK2"],
        dial_colors=["green"],
        watch_count=1,
        readable_text_confidence=0.95,
    )
    gmt = StoryProductCandidate(
        catalog_item_key="product:11379",
        product_id="11379",
        score=1.0,
        match_reasons=[
            "tray_brand_model:Baltic Aquascaphe verde",
            "listing:relogio baltic aquascaphe gmt automatico verde",
        ],
        source="tray_search",
    )
    status, top = classify_match([gmt], multiple_products=False, analysis=analysis)
    assert status != "matched"
    assert status == "ambiguous"
