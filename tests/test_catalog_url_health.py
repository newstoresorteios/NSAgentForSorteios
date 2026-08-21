from app.catalog_index_repository import row_to_product_dict
from app.product_media import normalize_storefront_brand_path


def test_normalize_storefront_brand_path_rewrites_bulova_slug():
    raw = (
        "https://www.newstorerj.com.br/relogios-bulova/"
        "relogio-seminovo-bulova-marine-star-serie-c-automatico-preto-96a288"
    )
    fixed = normalize_storefront_brand_path(raw)
    assert fixed is not None
    assert "/relogios/relogios-bulova/" in fixed
    assert normalize_storefront_brand_path(fixed) == fixed


def test_row_to_product_dict_rewrites_stale_url():
    product = row_to_product_dict(
        {
            "product_id": "15064",
            "catalog_item_key": "15064",
            "title_normalized": "bulova 96a288",
            "url": (
                "https://www.newstorerj.com.br/relogios-bulova/"
                "relogio-seminovo-bulova-marine-star-serie-c-automatico-preto-96a288"
            ),
            "price": 2941.17,
        }
    )
    assert "/relogios/relogios-bulova/" in str(product.get("url") or "")
