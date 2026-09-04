from types import SimpleNamespace

from app.catalog.index.catalog_index import trigram_similarity
from app.catalog.index.repository import CatalogIndexRepository


def test_trigram_similarity_recovers_sealander_typo() -> None:
    assert trigram_similarity("seelander", "sealander") >= 0.28
    assert trigram_similarity("samuray", "samurai") >= 0.28


def test_rank_lexical_prefers_typo_close_title() -> None:
    repo = CatalogIndexRepository()
    rows = [
        {
            "product_id": "wrong",
            "title_normalized": "Relógio Tissot PRX",
            "model": "PRX",
            "reference": "T137",
            "brand": "Tissot",
        },
        {
            "product_id": "hit",
            "title_normalized": "Relógio Christopher Ward Sealander",
            "model": "Sealander",
            "reference": "C63",
            "brand": "Christopher Ward",
        },
    ]
    ranked = repo._rank_lexical(rows, "seelander", limit=5)
    assert ranked
    assert ranked[0]["product_id"] == "hit"


def test_search_lexical_falls_back_when_trgm_sql_fails(monkeypatch) -> None:
    CatalogIndexRepository._pg_trgm_available = None
    repo = CatalogIndexRepository()
    calls: list[str] = []

    def fake_fetch(sql, params, *, swallow=True):
        calls.append(sql)
        if "similarity(" in sql:
            if not swallow:
                raise RuntimeError("undefined function similarity")
            return []
        return [
            {
                "product_id": "1",
                "title_normalized": "Relógio Seiko Prospex Sea Samurai",
                "model": "Sea Samurai",
                "reference": "SRPL13K1",
                "brand": "Seiko",
            }
        ]

    monkeypatch.setattr(repo, "_fetch", fake_fetch)
    rows = repo.search_lexical(
        tenant_id="newstore",
        query="samurai",
        brand="Seiko",
        limit=10,
    )
    assert any("similarity(" in sql for sql in calls)
    assert rows
    assert rows[0]["product_id"] == "1"
    CatalogIndexRepository._pg_trgm_available = None


def test_ensure_catalog_pg_trgm_skips_without_database(monkeypatch) -> None:
    import app.db as db

    monkeypatch.setattr(db, "_catalog_pg_trgm_ready", False)
    monkeypatch.setattr(db, "_catalog_pg_trgm_attempted", False)
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(database_url=""))
    assert db.ensure_catalog_pg_trgm() is False


def test_apply_catalog_pg_trgm_matches_migration() -> None:
    from pathlib import Path

    import app.db as db

    migration = Path("sql/025_catalog_index_pg_trgm.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in migration
    for name in (
        "idx_ai_catalog_index_title_trgm",
        "idx_ai_catalog_index_model_trgm",
        "idx_ai_catalog_index_reference_trgm",
    ):
        assert name in migration
        assert name in db._CATALOG_PG_TRGM_INDEX_SQL
