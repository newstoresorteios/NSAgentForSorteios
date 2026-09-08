"""Offline failure contracts; no production database or model calls."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import json
import sqlite3

import pytest

import app.learning.attendance_learning as batch
import app.learning.cursor as cursor


@pytest.fixture
def isolated_batch(monkeypatch):
    mocks = {}
    monkeypatch.setattr(batch, "get_settings", lambda: SimpleNamespace(agent_persona_tenant_id="audit"))
    values = {
        "evaluate_canaries": {}, "load_cursor": {"last_response_id": 10},
        "fetch_attendances_since": [], "save_cursor": None,
        "fetch_recent_reviews_for_cluster": [], "persist_attendance_review": (1, True),
        "compute_fail_rate": (0.0, 0), "promote_insights_to_extensions": 1,
        "upsert_learning_insight": 1,
    }
    for name, value in values.items():
        mocks[name] = Mock(return_value=value)
        monkeypatch.setattr(batch, name, mocks[name])
    mocks["reflect_cluster"] = AsyncMock(return_value=None)
    monkeypatch.setattr(batch, "reflect_cluster", mocks["reflect_cluster"])
    return mocks


@pytest.mark.asyncio
@pytest.mark.parametrize("failing,code", [
    ("load_cursor", "cursor_load_failed"),
    ("fetch_attendances_since", "attendance_fetch_failed"),
    ("save_cursor", "cursor_save_failed"),
    ("fetch_recent_reviews_for_cluster", "cluster_reviews_fetch_failed"),
    ("evaluate_canaries", "canary_evaluation_failed"),
])
async def test_failed_read_or_cursor_save_never_promotes(isolated_batch, failing, code):
    isolated_batch[failing].side_effect = RuntimeError("offline database unavailable")
    result = await batch.run_attendance_learning_batch(auto_promote=True)
    assert result["ok"] is False
    assert code in result.get("errors", [result.get("error")])
    isolated_batch["promote_insights_to_extensions"].assert_not_called()
    isolated_batch["reflect_cluster"].assert_not_awaited()
    if failing in {"load_cursor", "fetch_attendances_since"}:
        isolated_batch["save_cursor"].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [(RuntimeError("write failed"), "review_persist_failed"), (None, "review_persist_missing_id")])
async def test_persist_failure_advances_only_successful_prefix(isolated_batch, failure, code):
    now = datetime.now(timezone.utc)
    isolated_batch["fetch_attendances_since"].return_value = [
        {"response_id": i, "response_created_at": now, "customer_text": "Olá", "agent_reply": "Olá"}
        for i in [11, 12, 13]
    ]
    isolated_batch["persist_attendance_review"].side_effect = [(101, True), failure, (103, True)]
    result = await batch.run_attendance_learning_batch(auto_promote=True)
    assert result["ok"] is False and code in result["errors"]
    assert result["cursor_to"] == 11
    assert isolated_batch["persist_attendance_review"].call_count == 2
    assert isolated_batch["save_cursor"].call_args.kwargs["last_response_id"] == 11
    isolated_batch["promote_insights_to_extensions"].assert_not_called()


@pytest.mark.asyncio
async def test_existing_review_is_successful_prefix_without_relearning(isolated_batch):
    isolated_batch["fetch_attendances_since"].return_value = [{"response_id": 11, "agent_reply": "Olá"}]
    isolated_batch["persist_attendance_review"].return_value = (101, False)
    result = await batch.run_attendance_learning_batch(auto_promote=True)
    assert result["ok"] is True and result["cursor_to"] == 11
    assert result["reviews_written"] == 0
    isolated_batch["reflect_cluster"].assert_not_awaited()


@pytest.mark.parametrize("last_id", [None, 10])
def test_fetch_projection_works_without_response_metadata_column(monkeypatch, last_id):
    # SQLite executes the actual SELECT and JOIN against the older physical
    # columns. Only DBAPI placeholders and PostgreSQL's JSONB cast are adapted.
    # This does not claim PostgreSQL/RLS validation.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS public")
    db.execute("CREATE TABLE public.ai_inbound_messages (id integer, text text, channel text, conversation_id text, sender_phone text)")
    db.execute("CREATE TABLE public.ai_agent_responses (id integer, inbound_id integer, reply_text text, intent text, handoff_required boolean, safety_reason text, provider_response text, created_at text, sender_key text)")
    db.execute("INSERT INTO public.ai_inbound_messages VALUES (1, 'Olá', 'whatsapp', 'audit', 'audit')")
    for response_id, provider in [(11, {"_agent_context": {"source": "legacy"}}),
                                  (12, {"_agent_metadata": {"source": "current"}, "_agent_context": {"source": "legacy"}}),
                                  (13, {})]:
        db.execute("INSERT INTO public.ai_agent_responses VALUES (?, 1, 'Olá', 'general', 0, NULL, ?, ?, 'audit')",
                   (response_id, json.dumps(provider), datetime.now(timezone.utc).isoformat()))

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params):
            assert "response.response_metadata" not in sql
            self.result = db.execute(sql.replace("%s", "?").replace("::jsonb", ""),
                                     tuple(value.isoformat() if isinstance(value, datetime) else value for value in params))
        def fetchall(self):
            result = []
            for row in self.result.fetchall():
                item = dict(row)
                item["response_metadata"] = json.loads(item["response_metadata"])
                result.append(item)
            return result

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()

    monkeypatch.setattr(cursor, "get_conn", Conn)
    try:
        rows = cursor.fetch_attendances_since(tenant_id="audit", last_response_id=last_id, limit=10, bootstrap_hours=24)
        assert [row["response_id"] for row in rows] == [11, 12, 13]
        assert [row["response_metadata"] for row in rows] == [{"source": "legacy"}, {"source": "current"}, {}]
        assert all(row["customer_text"] == "Olá" for row in rows)
    finally:
        db.close()
