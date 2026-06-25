from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator
import psycopg
from psycopg.rows import dict_row
from .config import get_settings


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_tables() -> None:
    settings = get_settings()
    if not settings.auto_create_tables:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_inbound_messages (
                  id bigserial PRIMARY KEY,
                  provider text NOT NULL DEFAULT 'brevo',
                  event_type text NULL,
                  message_id text NULL,
                  conversation_id text NULL,
                  sender_phone text NULL,
                  sender_name text NULL,
                  text text NOT NULL DEFAULT '',
                  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS ai_agent_responses (
                  id bigserial PRIMARY KEY,
                  inbound_id bigint NULL REFERENCES ai_inbound_messages(id) ON DELETE SET NULL,
                  sender_phone text NULL,
                  reply_text text NOT NULL,
                  intent text NULL,
                  handoff_required boolean NOT NULL DEFAULT false,
                  safety_reason text NULL,
                  provider_send_ok boolean NULL,
                  provider_response jsonb NULL,
                  created_at timestamptz NOT NULL DEFAULT now()
                );
                """
            )


def insert_inbound_message(message: dict[str, Any]) -> int | None:
    if not get_settings().database_url:
        return None
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_inbound_messages
                  (provider, event_type, message_id, conversation_id, sender_phone, sender_name, text, raw)
                VALUES
                  (%(provider)s, %(event_type)s, %(message_id)s, %(conversation_id)s, %(sender_phone)s, %(sender_name)s, %(text)s, %(raw)s)
                RETURNING id
                """,
                message,
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None


def insert_agent_response(data: dict[str, Any]) -> int | None:
    if not get_settings().database_url:
        return None
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_agent_responses
                  (inbound_id, sender_phone, reply_text, intent, handoff_required, safety_reason, provider_send_ok, provider_response)
                VALUES
                  (%(inbound_id)s, %(sender_phone)s, %(reply_text)s, %(intent)s, %(handoff_required)s, %(safety_reason)s, %(provider_send_ok)s, %(provider_response)s)
                RETURNING id
                """,
                data,
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None
