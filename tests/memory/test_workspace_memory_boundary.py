from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.memory import contact_memory_repository as contacts
from app.memory import conversation_summary_repository as summaries
from app.memory import memory_proposal_repository as proposals
from app.memory.workspace_scope import memory_workspace
from app.persona import persona_repository, prompt_trace_repository
from app.persona.persona_runtime import set_persona_runtime, reset_persona_runtime

WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params):
        assert sql.count("%s") == len(params), "SQL placeholder/parameter mismatch"
        self.calls.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def __enter__(self): return self
    def __exit__(self, *args): pass


def connection(cursor):
    @contextmanager
    def connect():
        yield SimpleNamespace(cursor=lambda: cursor)
    return connect


@pytest.fixture(autouse=True)
def no_inherited_runtime():
    token = set_persona_runtime(None)
    yield
    reset_persona_runtime(token)


def test_unscoped_memory_cannot_read_legacy_or_write_new_rows(monkeypatch):
    def forbidden(): raise AssertionError("database must not be touched")
    monkeypatch.setattr(contacts, "get_conn", forbidden)
    monkeypatch.setattr(summaries, "get_conn", forbidden)
    assert contacts.get_active_contact_memories(tenant_id="store", sender_key="same-contact") == []
    assert summaries.get_conversation_summary(tenant_id="store", conversation_key="same-thread") is None
    with pytest.raises(ValueError, match="workspace_required"):
        contacts.upsert_contact_memory(tenant_id="store", sender_key="same-contact", memory_key="color",
                                       memory_kind="color_preference", value="blue")


def test_runtime_scope_cannot_be_replaced_by_another_workspace():
    token = set_persona_runtime(SimpleNamespace(workspace_id=WORKSPACE))
    try:
        assert memory_workspace() == WORKSPACE
        with pytest.raises(ValueError, match="workspace_mismatch"):
            memory_workspace(OTHER)
    finally:
        reset_persona_runtime(token)


def test_turn_cache_keeps_resolved_workspaces_separate(monkeypatch):
    from app.core.turn_cache import begin_turn_cache, end_turn_cache
    cursor = Cursor()
    monkeypatch.setattr(contacts, "get_conn", connection(cursor))
    cache = begin_turn_cache()
    try:
        for workspace in (WORKSPACE, OTHER, WORKSPACE):
            token = set_persona_runtime(SimpleNamespace(workspace_id=workspace))
            try:
                contacts.get_active_contact_memories(tenant_id="store", sender_key="same")
            finally:
                reset_persona_runtime(token)
        assert [params[1] for _, params in cursor.calls] == [WORKSPACE, OTHER]
    finally:
        end_turn_cache(cache)


def test_contact_and_summary_queries_include_workspace_for_same_contact(monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(contacts, "get_conn", connection(cursor))
    monkeypatch.setattr(summaries, "get_conn", connection(cursor))
    contacts.get_active_contact_memories(tenant_id="store", workspace_id=WORKSPACE, sender_key="same")
    summaries.get_conversation_summary(tenant_id="store", workspace_id=OTHER, conversation_key="same")
    assert "workspace_id = %s::uuid" in cursor.calls[0][0]
    assert cursor.calls[0][1][1] == WORKSPACE
    assert "workspace_id = %s::uuid" in cursor.calls[1][0]
    assert cursor.calls[1][1][1] == OTHER


def test_postgres_uuid_scope_is_accepted_in_memory_rows():
    from uuid import UUID
    memory = contacts._row_to_memory({"tenant_id": "store", "workspace_id": UUID(WORKSPACE),
        "sender_key": "same", "memory_key": "color", "memory_kind": "color_preference"})
    assert memory.workspace_id == WORKSPACE


def test_proposal_idempotency_and_review_are_scoped(monkeypatch):
    cursor = Cursor([{"id": 4}])
    monkeypatch.setattr(proposals, "get_conn", connection(cursor))
    assert proposals.insert_memory_proposal(tenant_id="store", workspace_id=WORKSPACE,
            proposal_type="memory", target_scope="contact", idempotency_key="same") == 4
    sql, params = cursor.calls[0]
    assert "workspace_id" in sql and params[1] == WORKSPACE
    assert WORKSPACE + ":same" in params
    proposals.mark_proposal_applied(4, workspace_id=WORKSPACE, applied_memory_id=9)
    assert "AND workspace_id = %s::uuid" in cursor.calls[-1][0]
    assert cursor.calls[-1][1][-1] == WORKSPACE


def test_prompt_audit_records_scope_and_only_links_matching_response(monkeypatch):
    cursor = Cursor([{"id": 7}])
    monkeypatch.setattr(persona_repository, "get_conn", connection(cursor))
    monkeypatch.setattr(prompt_trace_repository, "get_conn", connection(cursor))
    persona_repository.insert_prompt_compilation(tenant_id="store", workspace_id=WORKSPACE,
        compiled_instructions_hash="hash", openai_api_mode="responses", inbound_id=2)
    assert cursor.calls[0][1][1] == WORKSPACE
    prompt_trace_repository.link_prompt_response(workspace_id=WORKSPACE, inbound_id=2, response_id=3)
    sql, params = cursor.calls[-1]
    assert "r.workspace_id = p.workspace_id" in sql and "r.inbound_id = p.inbound_id" in sql
    assert params == (WORKSPACE, 2, 3)
