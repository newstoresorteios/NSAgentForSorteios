from __future__ import annotations

from app.instagram_story_models import StoryVisualUnderstanding
from app.story_product_matcher import (
    edition_count_search_tokens,
    extract_store_product_url,
    tokens_from_store_url,
    tray_search_jobs,
)


def test_tokens_from_bulova_breton_store_url():
    brand, tokens = tokens_from_store_url(
        "https://www.newstorerj.com/relogios/relogio-seminovo-bulova-breton-automatico-96b332"
    )
    assert brand and brand.casefold() == "bulova"
    folded = {t.casefold() for t in tokens}
    assert "breton" in folded
    assert "96b332" in folded


def test_edition_count_from_limited_story_copy():
    analysis = StoryVisualUnderstanding(
        visible_brands=["Bulova"],
        visible_text=[
            "Seminovo e limitado a apenas 350 unidades no mundo.",
            "CONFIRA",
            "BULOVA",
            "1875",
        ],
    )
    assert edition_count_search_tokens(analysis) == ["350"]
    jobs = tray_search_jobs(analysis)
    assert any(
        (brand or "").casefold() == "bulova" and tokens == ["350"] for brand, tokens in jobs
    )


def test_extract_store_product_url_from_pasted_text():
    url = extract_store_product_url(
        "esse aqui https://www.newstorerj.com/relogios/relogio-seminovo-bulova-breton-automatico-96b332 valeu"
    )
    assert url and "96b332" in url.casefold()
