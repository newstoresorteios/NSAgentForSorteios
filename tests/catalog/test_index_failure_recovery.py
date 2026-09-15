from types import SimpleNamespace
from app.catalog.index.repository import CatalogIndexRepository


def test_transient_failure_does_not_poison_next_fuzzy_query(monkeypatch):
    monkeypatch.setattr(CatalogIndexRepository, "_pg_trgm_available", None)
    attempts = []
    def fetch(sql, params, **kwargs):
        attempts.append(sql)
        if "similarity(" in sql and len(attempts) == 1:
            raise TimeoutError("temporary")
        return []
    repo = CatalogIndexRepository()
    monkeypatch.setattr(repo, "_fetch", fetch)
    repo.search_lexical(tenant_id="tenant", query="sealander")
    repo.search_lexical(tenant_id="tenant", query="sealander")
    assert sum("similarity(" in sql for sql in attempts) == 2


def test_available_products_are_filtered_before_limit(monkeypatch):
    import sqlite3
    import re
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE products(product_id,tenant_id,available,stock,price,promotional_price,freshness_at)")
    db.executemany("INSERT INTO products VALUES(?,?,?,?,?,?,?)", [(i,"tenant",0,0,100,None,100-i) for i in range(40)])
    db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)", (99,"tenant",1,2,200,150,1))
    monkeypatch.setattr("app.catalog.index.repository._ttl_cutoff", lambda:None)
    repo = CatalogIndexRepository()
    def fetch(sql, params, **kwargs):
        sql = re.sub(r"%\((\w+)\)s", r":\1", sql.replace("public.ai_catalog_index", "products"))
        return [dict(r) for r in db.execute(sql, params)]
    monkeypatch.setattr(repo, "_fetch", fetch)
    assert [r["product_id"] for r in repo.search_by_constraints(tenant_id="tenant", max_price=175, limit=3)] == [99]
    db.close()
