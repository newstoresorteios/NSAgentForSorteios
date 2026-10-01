from __future__ import annotations

from app.memory.memory_consolidation import consolidate_contact_memories
from app.memory import memory_consolidation as consolidation
from app.persona.persona_runtime import set_persona_runtime, reset_persona_runtime
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def no_runtime():
    token = set_persona_runtime(None)
    yield
    reset_persona_runtime(token)


def test_consolidate_returns_counts_without_db(monkeypatch):
    import app.memory.memory_consolidation as consolidation

    monkeypatch.setattr(consolidation, "expire_contact_memories", lambda **_k: 2)
    monkeypatch.setattr(consolidation, "prune_excess_active_memories", lambda **_k: 1)
    monkeypatch.setattr(
        consolidation,
        "get_settings",
        lambda: type("S", (), {"agent_persona_tenant_id": "newstore"})(),
    )
    result = consolidate_contact_memories(
        tenant_id="newstore",
        workspace_id=WORKSPACE,
        sender_key="whatsapp:1",
    )
    assert result["expired"] == 2
    assert result["pruned"] == 1
    assert result["tenant_id"] == "newstore"
    assert result["workspace_id"] == WORKSPACE


@pytest.fixture
def memory_database(monkeypatch):
    """Execute the actual repository predicates with synthetic two-workspace rows.

    Only PostgreSQL's placeholder/UUID/ANY syntax is adapted for in-memory SQLite;
    filtering, ordering and UPDATE predicates are evaluated by a SQL engine.
    """
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("""CREATE TABLE ai_contact_memories (
        id integer PRIMARY KEY, tenant_id text, workspace_id text, sender_key text,
        scope_status text, status text, expires_at text, importance real,
        last_confirmed_at text, use_in_instructions boolean, updated_at text)""")
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    records = [
        (1, "store", WORKSPACE, "same", "verified", "active", expired, 0.9),
        (2, "store", WORKSPACE, "same", "verified", "active", None, 0.1),
        (3, "store", OTHER, "same", "verified", "active", expired, 0.95),
        (4, "store", OTHER, "same", "verified", "active", None, 0.2),
        (5, "store", None, "same", "quarantined", "active", expired, 0.99),
        (6, "store", WORKSPACE, "same", "quarantined", "active", expired, 0.01),
        (7, "different-tenant", WORKSPACE, "same", "verified", "active", expired, 0.01),
    ]
    database.executemany("INSERT INTO ai_contact_memories VALUES (?,?,?,?,?,?,?,?,NULL,true,NULL)", records)

    class Cursor:
        def __init__(self):
            self.cursor = database.cursor()

        def execute(self, sql, params):
            sql = sql.replace("public.", "").replace("::uuid", "")
            sql = sql.replace("id = ANY(%s)", "id IN (SELECT value FROM json_each(%s))").replace("%s", "?")
            values = [value.isoformat() if isinstance(value, datetime) else
                      json.dumps(value) if isinstance(value, list) else value for value in params]
            self.cursor.execute(sql, values)

        @property
        def rowcount(self):
            return self.cursor.rowcount

        def fetchall(self):
            return self.cursor.fetchall()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    @contextmanager
    def conn():
        from types import SimpleNamespace
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(consolidation, "get_conn", conn)
    yield database
    database.close()


def test_expiration_only_changes_verified_rows_in_selected_workspace(memory_database):
    changed = consolidation.expire_contact_memories(tenant_id="store", workspace_id=WORKSPACE)
    assert changed == 1
    states = {row["id"]: row["status"] for row in memory_database.execute("SELECT id,status FROM ai_contact_memories")}
    assert states == {1: "expired", 2: "active", 3: "active", 4: "active", 5: "active", 6: "active", 7: "active"}


def test_pruning_does_not_mix_same_sender_across_workspaces_or_quarantine(memory_database):
    assert consolidation.prune_excess_active_memories(
        tenant_id="store", workspace_id=WORKSPACE, sender_key="same", keep=1,
    ) == 1
    states = {row["id"]: row["status"] for row in memory_database.execute("SELECT id,status FROM ai_contact_memories")}
    assert states == {1: "active", 2: "superseded", 3: "active", 4: "active", 5: "active", 6: "active", 7: "active"}
    assert consolidation.prune_excess_active_memories(
        tenant_id="store", workspace_id=OTHER, sender_key="same", keep=1,
    ) == 1
    assert memory_database.execute("SELECT status FROM ai_contact_memories WHERE id=4").fetchone()[0] == "superseded"
    assert memory_database.execute("SELECT status FROM ai_contact_memories WHERE id=1").fetchone()[0] == "active"


@pytest.mark.parametrize("operation", [
    lambda: consolidation.expire_contact_memories(tenant_id="store"),
    lambda: consolidation.prune_excess_active_memories(tenant_id="store", sender_key="same"),
    lambda: consolidation.consolidate_contact_memories(tenant_id="store", sender_key="same"),
])
def test_consolidation_without_workspace_fails_before_database_access(monkeypatch, operation):
    monkeypatch.setattr(consolidation, "get_conn", lambda: pytest.fail("unscoped database access"))
    with pytest.raises(ValueError, match="memory_workspace_required"):
        operation()
