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


def test_mark_stale_or_zero_price_unavailable_sql_shape(monkeypatch):
    """Ensure hygiene update runs with bound params (no string-built intervals)."""
    import app.catalog_url_health as module
    import app.db as db_mod

    captured: dict = {}

    class FakeCursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"catalog_item_key": "1"}]

    class FakeCursorCtx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursorCtx()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(module, "_tenant_id", lambda: "newstore")
    monkeypatch.setattr(db_mod, "get_conn", lambda: FakeConn())

    result = module.mark_stale_or_zero_price_unavailable(stale_days=3, limit=10)
    assert result["ok"] is True
    assert result["marked_unavailable"] == 1
    assert "make_interval(days => %(days)s)" in captured["sql"]
    assert captured["params"]["days"] == 3
    assert captured["params"]["limit"] == 10
