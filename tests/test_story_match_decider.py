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
    assert "ward" not in folded or "sealander" in folded
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
    assert winner is not None
    assert winner.product_id == "10611"


def test_classify_match_resolves_cw_tie_with_analysis():
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
    assert status == "matched"
    assert top is not None
    assert top.product_id == "10611"
