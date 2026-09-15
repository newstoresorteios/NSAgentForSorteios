"""Exercise production SQL against temporary tables; always roll back.

Usage: python scripts/verify_quality_postgres.py --db-env-file PATH --migration PATH
No customer row, configuration or public sequence is changed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(connection, migration: Path):
    tables = ("conversas", "ai_inbound_messages", "agent_configuration_catalog")
    with connection.cursor() as cur:
        for table in tables:
            cur.execute(f"CREATE TEMP TABLE quality_{table} AS SELECT * FROM public.{table} WITH NO DATA")
        cur.execute("CREATE UNIQUE INDEX ON quality_agent_configuration_catalog (key)")
        cur.execute("""CREATE TEMP TABLE quality_conversation_reconciliation_audit (
            workspace_id uuid, entity_type text CHECK(entity_type IN ('conversation','message')),
            entity_id uuid, destination_id uuid, original_row jsonb)""")

    class Cursor:
        def __init__(self):
            self.inner = connection.cursor()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.inner.close()
        def execute(self, sql, params=None):
            for table in (*tables, "conversation_reconciliation_audit"):
                sql = sql.replace("public." + table, "pg_temp.quality_" + table)
            assert "public." not in sql, "Public access prohibited in the SQL exercise"
            self.inner.execute(sql, params)
        def fetchone(self):
            return self.inner.fetchone()
        def fetchall(self):
            return self.inner.fetchall()

    class Connection:
        def cursor(self):
            return Cursor()
        def commit(self):
            pass  # Deliberately leave the outer transaction uncommitted.

    @contextmanager
    def get_conn():
        yield Connection()

    owner, other = str(uuid4()), str(uuid4())
    ids = [str(uuid4()) for _ in range(4)]
    with connection.cursor() as cur:
        for index, (workspace, channel, thread) in enumerate([
            (owner, "whatsapp", "incident"), (other, "whatsapp", "incident"),
            (owner, "instagram", "incident"), (owner, "whatsapp", "another-session"),
        ]):
            cur.execute("""INSERT INTO quality_conversas
                (id, workspace_id, channel, external_thread_id, status, bot_activated, canal_id, updated_at)
                VALUES (%s,%s,%s,%s,'active',true,'test-channel',now())""", (ids[index],workspace,channel,thread))
        cur.execute("""INSERT INTO quality_ai_inbound_messages(id,workspace_id,channel,conversation_id)
            VALUES (987654321,%s,'whatsapp','incident')""", (owner,))

    from app.models import IncomingMessage
    from app.ops.handoff_queue import mark_conversa_for_human_handoff
    with patch("app.ops.handoff_queue.get_conn", get_conn):
        incoming = IncomingMessage(text="fixture", channel="whatsapp", conversation_id="incident", raw={"inbound_id":987654321})
        assert mark_conversa_for_human_handoff(incoming, reason="quality_test") == [ids[0]]
        assert mark_conversa_for_human_handoff(incoming, reason="quality_test") == [ids[0]]
    with connection.cursor() as cur:
        cur.execute("SELECT id,status,bot_activated FROM quality_conversas ORDER BY id")
        for row in cur.fetchall():
            assert (row["status"], row["bot_activated"]) == (("waiting",False) if str(row["id"]) == ids[0] else ("active",True))
        cur.execute("SELECT original_row FROM quality_conversation_reconciliation_audit")
        audit = cur.fetchall()
        assert len(audit) == 1 and audit[0]["original_row"]["bot_activated"] is True
        assert audit[0]["original_row"]["_handoff"]["inbound_id"] == 987654321
        sql = migration.read_text(encoding="utf-8").replace("BEGIN;", "").replace("COMMIT;", "")
        sql = sql.replace("public.agent_configuration_catalog", "pg_temp.quality_agent_configuration_catalog")
        assert "public." not in sql
        cur.execute(sql)
        cur.execute(sql)
        cur.execute("SELECT count(*) AS count FROM quality_agent_configuration_catalog")
        assert cur.fetchone()["count"] == 18
    return {"handoff_isolation":True,"before_image_audit":True,"handoff_idempotence":True,
            "migration_definitions":18,"migration_idempotence":True,"public_writes":0,"transaction":"rolled_back"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-env-file", type=Path, required=True)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    env = dotenv_values(args.db_env_file)
    dsn = env.get("DATABASE_URL") or env.get("SUPABASE_DB_URL")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as connection:
        try:
            result = verify(connection, args.migration)
        finally:
            connection.rollback()
    if args.output:
        args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
