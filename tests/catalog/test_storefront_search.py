from app.stories.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from io import BytesIO

from PIL import Image

from app.catalog.media.storefront_search import (
    best_image_view_metrics,
    parse_storefront_search_html,
    perceptual_image_hash,
)
from app.stories.story_match_decider import try_resolve_tied_candidates
from app.stories.story_product_matcher import _candidate_core_listing_key, classify_match


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


def test_parse_storefront_search_html_reads_rich_tray_product_data():
    html = r'''<script>dataLayer = [{"listProducts":[
      {"idProduct":"16010","nameProduct":"Rel\u00f3gio Hamilton Khaki Field Murph Autom\u00e1tico Azul H70405740 38 mm","sellPrice":"8599.99","reference":"H70405740","model":"Hamilton Khaki Field Murph","urlImage":"https:\/\/images.example\/murph.jpg","urlProduct":"http:\/\/www.newstorerj.com.br\/relogios\/murph-h70405740"}
    ],"filter":{}}]</script>'''

    hits = parse_storefront_search_html(html)

    assert hits == [{
        "product_id": "16010",
        "name": "Relógio Hamilton Khaki Field Murph Automático Azul H70405740 38 mm",
        "reference": "H70405740",
        "brand": "",
        "model": "Hamilton Khaki Field Murph",
        "url": "https://www.newstorerj.com.br/relogios/murph-h70405740",
        "image_url": "https://images.example/murph.jpg",
        "price": "8599.99",
    }]


def test_perceptual_hash_survives_resize_but_separates_another_image():
    source = Image.new("RGB", (80, 80), "white")
    for x in range(15, 45):
        for y in range(20, 65):
            source.putpixel((x, y), (10, 40, 160))
    resized = source.resize((160, 160))
    other = Image.new("RGB", (80, 80), "white")
    for x in range(45, 70):
        for y in range(5, 35):
            other.putpixel((x, y), (160, 40, 10))

    def encoded(image):
        stream = BytesIO()
        image.save(stream, format="JPEG", quality=85)
        return stream.getvalue()

    source_hash = perceptual_image_hash(encoded(source))
    resized_hash = perceptual_image_hash(encoded(resized))
    other_hash = perceptual_image_hash(encoded(other))

    assert (source_hash ^ resized_hash).bit_count() <= 6
    assert (source_hash ^ other_hash).bit_count() > 6


def test_center_crop_can_match_the_same_catalog_image():
    image = Image.new("RGB", (240, 320), "white")
    for x in range(55, 185):
        for y in range(75, 245):
            image.putpixel((x, y), (25, 25, 25))
    for x in range(78, 162):
        for y in range(118, 202):
            image.putpixel((x, y), (165, 25, 25))
    detail = image.crop((48, 88, 192, 232)).resize((300, 300))

    def encoded(value):
        stream = BytesIO()
        value.save(stream, format="PNG")
        return stream.getvalue()

    distance, color_error, source_view, catalog_view = best_image_view_metrics(
        encoded(detail), encoded(image)
    )

    assert distance <= 6
    assert color_error <= 0.035
    assert source_view != catalog_view


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
