from contextlib import contextmanager
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.configuration.workspace import resolve_conversation_workspace


def install_repository(monkeypatch, rows):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query, params):
            pass

        def fetchall(self):
            return rows

    @contextmanager
    def get_conn():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url="configured"))
    monkeypatch.setattr("app.db.get_conn", get_conn)


@pytest.mark.parametrize("conversation_id,database_url", [(None, "configured"), ("", "configured"), ("conversation", "")])
def test_missing_identity_or_database_does_not_query(monkeypatch, conversation_id, database_url):
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url=database_url))

    def unexpected_connection():
        pytest.fail("No database lookup should occur without identity and database")

    monkeypatch.setattr("app.db.get_conn", unexpected_connection)
    assert resolve_conversation_workspace(conversation_id, "whatsapp") is None


def test_unknown_conversation_has_no_workspace(monkeypatch):
    install_repository(monkeypatch, [])
    assert resolve_conversation_workspace("missing", "whatsapp") is None


def test_postgres_uuid_is_returned_as_workspace_string(monkeypatch):
    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    install_repository(monkeypatch, [{"workspace_id": workspace_id}])
    assert resolve_conversation_workspace("conversation", "whatsapp") == str(workspace_id)


def test_ambiguous_conversation_never_selects_an_arbitrary_workspace(monkeypatch):
    install_repository(monkeypatch, [{"workspace_id": uuid4()}, {"workspace_id": uuid4()}])
    with pytest.raises(ValueError, match="ambiguous_conversation_workspace"):
        resolve_conversation_workspace("shared-external-id", None)


@pytest.fixture
def readonly_postgres(monkeypatch):
    """Explicit opt-in; never inherit the application DATABASE_URL automatically.

    The real PostgreSQL binding protocol is needed to catch SQLSTATE 42P18.
    Only SELECTs run, enforced by a read-only transaction, with no customer payloads.
    """
    database_url = os.getenv("NSAGENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set NSAGENT_TEST_DATABASE_URL to run PostgreSQL contract checks")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, connect_timeout=10, prepare_threshold=None, row_factory=dict_row) as conn:
        conn.read_only = True
        conn.execute("SELECT set_config('statement_timeout', '5000', true)")

        @contextmanager
        def get_conn():
            yield conn

        monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url="configured"))
        monkeypatch.setattr("app.db.get_conn", get_conn)
        yield conn


@pytest.mark.integration
@pytest.mark.parametrize("channel", ["whatsapp", "instagram", "", None])
def test_postgres_binds_optional_channel_for_unknown_conversation(readonly_postgres, channel):
    # A string also has an unknown parameter type in psycopg until SQL infers it.
    assert resolve_conversation_workspace(f"sql-contract-probe:{uuid4()}", channel) is None


@pytest.mark.integration
def test_postgres_resolves_existing_conversation_and_respects_channel(readonly_postgres):
    row = readonly_postgres.execute("""
        SELECT id::text, workspace_id::text, channel
        FROM public.conversas WHERE workspace_id IS NOT NULL LIMIT 1
    """).fetchone()
    if not row:
        pytest.skip("No conversation available for the read-only ownership contract")
    assert resolve_conversation_workspace(row["id"], row["channel"]) == row["workspace_id"]
    assert resolve_conversation_workspace(row["id"], None) == row["workspace_id"]
    assert resolve_conversation_workspace(row["id"], "sql-contract-unmatched-channel") is None
