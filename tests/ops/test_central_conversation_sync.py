from contextlib import contextmanager

from app.configuration import workspace
from app.ops.central_conversation_sync import sync_agent_conversation


class _Cursor:
    def __init__(self):
        self.queries = []
        self.rowcount = 0
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        normalized = " ".join(query.split())
        self.queries.append((normalized, params))
        self.rowcount = 0
        self._row = None
        if "SELECT conversation_id, channel" in normalized:
            self._row = {
                "conversation_id": "ig:customer-1",
                "channel": "instagram",
                "customer_name": "cliente",
            }
        elif normalized.startswith("SELECT id FROM public.conversas"):
            self._row = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        elif "INSERT INTO public.mensagens" in normalized:
            self.rowcount = 1 if "response.reply_text" in normalized else 2

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.cur = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cur


def test_sync_reconciles_all_known_messages_idempotently(monkeypatch):
    connection = _Connection()

    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr("app.db.get_conn", fake_conn)
    monkeypatch.setattr("app.ops.central_conversation_sync.log_event", lambda *_a: None)

    result = sync_agent_conversation(977, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    assert result == {
        "ok": True,
        "conversation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "inbound_inserted": 2,
        "outbound_inserted": 1,
    }
    sql = "\n".join(query for query, _params in connection.cur.queries)
    assert "inbound.conversation_id = %(conversation_id)s" in sql
    assert "response.provider_send_ok = true" in sql
    assert sql.count("ON CONFLICT (external_id)") == 2
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert "NULLIF(conversation.last_message, '') IS NULL" in sql


def test_workspace_stamp_triggers_central_sync_without_blocking(monkeypatch):
    connection = _Connection()

    @contextmanager
    def fake_conn():
        yield connection

    calls = []
    monkeypatch.setattr("app.db.get_conn", fake_conn)
    monkeypatch.setattr(
        "app.ops.central_conversation_sync.sync_agent_conversation",
        lambda inbound_id, workspace_id: calls.append((inbound_id, workspace_id)),
    )
    monkeypatch.setattr(connection, "commit", lambda: None, raising=False)

    workspace.stamp_inbound_workspace(42, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    assert calls == [(42, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")]
