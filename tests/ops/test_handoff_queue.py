import json
import sqlite3
from contextlib import contextmanager

import pytest

from app.models import IncomingMessage
from app.ops import handoff_queue


class Cursor:
    def __init__(self, db):
        self.db = db
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, params=()):
        sql = sql.replace("public.", "").replace("::uuid", "").replace("::text", "").replace("%s", "?").replace("FOR UPDATE", "")
        self.result = self.db.execute(sql, params)
    def fetchall(self):
        return [dict(row) for row in self.result.fetchall()]
    def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row else None


@pytest.fixture
def db(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE conversas(id TEXT PRIMARY KEY, workspace_id TEXT, channel TEXT,
          canal_id TEXT, external_thread_id TEXT, contact_phone TEXT, merged_into TEXT,
          status TEXT, bot_activated BOOLEAN, assigned_to TEXT, updated_at TEXT, handoff_requested_at TEXT, handoff_reason TEXT);
        CREATE TABLE ai_inbound_messages(id INTEGER, workspace_id TEXT, conversation_id TEXT, channel TEXT);
        CREATE TABLE conversation_reconciliation_audit(workspace_id TEXT, entity_type TEXT,
          entity_id TEXT, destination_id TEXT, original_row TEXT);
    """)
    db.execute("INSERT INTO ai_inbound_messages VALUES (1,'w1','thread','whatsapp')")
    @contextmanager
    def connection():
        class Connection:
            def cursor(self): return Cursor(db)
            def commit(self): db.commit()
        yield Connection()
    monkeypatch.setattr(handoff_queue, "get_conn", connection)
    monkeypatch.setattr(handoff_queue, "to_jsonb", lambda value: json.dumps(value, default=str))
    yield db
    db.close()


def add(db, id, workspace="w1", channel="whatsapp", thread="thread", merged=None, status="active", assigned=None, canal="c1"):
    db.execute("INSERT INTO conversas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (id, workspace, channel, canal, thread, "same-phone", merged, status, True, assigned, "before", None, None))


def incoming(**kwargs):
    kwargs.setdefault("raw", {"inbound_id":1})
    return IncomingMessage(channel="whatsapp", conversation_id="thread", sender_phone="same-phone", **kwargs)


def test_handoff_scoped_to_owner_channel_and_session_with_before_audit(db):
    add(db, "target")
    add(db, "other-workspace", workspace="w2")
    add(db, "other-channel", channel="instagram")
    add(db, "other-session", thread="old-thread")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(raw={"inbound_id": 1}), reason="customer_requested_human") == ["target"]
    changed = [r["id"] for r in db.execute("SELECT id FROM conversas WHERE bot_activated=false")]
    assert changed == ["target"]
    audit = json.loads(db.execute("SELECT original_row FROM conversation_reconciliation_audit").fetchone()[0])
    assert audit["status"] == "active"
    assert audit["bot_activated"] == 1
    assert audit["_handoff"]["reason"] == "customer_requested_human"


def test_ambiguous_workspace_or_channel_account_never_updates(db):
    add(db, "one")
    add(db, "two", canal="another-account")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_requested_human") == []
    assert db.execute("SELECT count(*) FROM conversas WHERE bot_activated=false").fetchone()[0] == 0


def test_missing_server_inbound_does_not_infer_owner_from_unique_thread(db):
    add(db, "only-conversation", workspace="other-workspace")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(raw={}), reason="customer_requested_human") == []


def test_phone_without_conversation_never_updates(db):
    add(db, "target")
    assert handoff_queue.mark_conversa_for_human_handoff(IncomingMessage(channel="whatsapp", sender_phone="same-phone"), reason="customer_requested_human") == []


def test_missing_inbound_owner_never_falls_back_to_phone(db):
    add(db, "target")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(raw={"inbound_id": 99}), reason="customer_requested_human") == []


def test_merged_alias_resolves_only_same_workspace_canonical(db):
    add(db, "canonical", thread="canonical-thread")
    add(db, "alias", merged="canonical", status="closed")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_requested_human") == ["canonical"]
    assert db.execute("SELECT status FROM conversas WHERE id='alias'").fetchone()[0] == "closed"


def test_cross_workspace_merge_is_rejected(db):
    add(db, "canonical", workspace="w2", thread="canonical-thread")
    add(db, "alias", merged="canonical", status="closed")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_requested_human") == []


def test_existing_operator_and_closed_conversation_are_preserved(db):
    add(db, "target", assigned="operator")
    handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_requested_human")
    row = db.execute("SELECT assigned_to,status FROM conversas WHERE id='target'").fetchone()
    assert tuple(row) == ("operator", "active")
    db.execute("UPDATE conversas SET status='closed',bot_activated=true")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_requested_human") == []


def test_unconfirmed_ai_failure_never_enters_queue(db):
    add(db, "target")
    assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="integration_failure") == []
    assert db.execute("SELECT status FROM conversas WHERE id='target'").fetchone()[0] == "active"
    assert db.execute("SELECT count(*) FROM conversation_reconciliation_audit").fetchone()[0] == 0


def test_confirmed_transfer_records_signal_and_is_idempotent(db):
    add(db, "target")
    for _ in range(2):
        assert handoff_queue.mark_conversa_for_human_handoff(incoming(), reason="customer_accepted_handoff_offer") == ["target"]
    row = db.execute("SELECT status,handoff_requested_at,handoff_reason FROM conversas WHERE id='target'").fetchone()
    assert row[0] == "waiting" and row[1] and row[2] == "customer_accepted_handoff_offer"
    assert db.execute("SELECT count(*) FROM conversation_reconciliation_audit").fetchone()[0] == 1
