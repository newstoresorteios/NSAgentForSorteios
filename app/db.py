from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .config import get_settings


def to_jsonb(value: Any, default: Any = None) -> Jsonb:
    """Convert Python dict/list/value to psycopg Jsonb wrapper."""
    if value is None:
        value = {} if default is None else default
    return Jsonb(value)


def get_returning_id(row: Any) -> int | None:
    if not row:
        return None

    if isinstance(row, dict):
        return int(row["id"])

    return int(row[0])


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
    if not settings.database_url or not settings.auto_create_tables:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.ai_inbound_messages (
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

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_sender_phone
                ON public.ai_inbound_messages(sender_phone);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_created_at
                ON public.ai_inbound_messages(created_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_agent_responses (
                  id bigserial PRIMARY KEY,
                  inbound_id bigint NULL REFERENCES public.ai_inbound_messages(id) ON DELETE SET NULL,
                  sender_phone text NULL,
                  reply_text text NOT NULL,
                  intent text NULL,
                  handoff_required boolean NOT NULL DEFAULT false,
                  safety_reason text NULL,
                  provider_send_ok boolean NOT NULL DEFAULT false,
                  provider_response jsonb NULL,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_inbound_id
                ON public.ai_agent_responses(inbound_id);

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_created_at
                ON public.ai_agent_responses(created_at DESC);
                """
            )


def insert_inbound_message(message: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_message = dict(message or {})

    safe_message.setdefault("provider", "brevo")
    safe_message.setdefault("event_type", None)
    safe_message.setdefault("message_id", None)
    safe_message.setdefault("conversation_id", None)
    safe_message.setdefault("sender_phone", None)
    safe_message.setdefault("sender_name", None)
    safe_message.setdefault("text", None)

    safe_message["raw"] = to_jsonb(safe_message.get("raw") or {})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_inbound_messages
                  (
                    provider,
                    event_type,
                    message_id,
                    conversation_id,
                    sender_phone,
                    sender_name,
                    text,
                    raw
                  )
                VALUES
                  (
                    %(provider)s,
                    %(event_type)s,
                    %(message_id)s,
                    %(conversation_id)s,
                    %(sender_phone)s,
                    %(sender_name)s,
                    %(text)s,
                    %(raw)s
                  )
                RETURNING id
                """,
                safe_message,
            )

            row = cur.fetchone()
            return get_returning_id(row)


def inbound_message_exists(provider: str | None, message_id: str | None) -> bool:
    """Return whether this provider message was already recorded.

    Missing IDs are intentionally never deduplicated because two identical texts
    can be legitimate separate messages.
    """
    settings = get_settings()
    if not settings.database_url or not provider or not message_id:
        return False

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.ai_inbound_messages
                WHERE provider = %(provider)s
                  AND message_id = %(message_id)s
                LIMIT 1
                """,
                {"provider": provider, "message_id": message_id},
            )
            return cur.fetchone() is not None


def claim_inbound_message(message: dict[str, Any]) -> tuple[bool, int | None]:
    """Atomically claim an inbound message using a PostgreSQL transaction lock."""
    settings = get_settings()
    if not settings.database_url:
        return True, None

    safe_message = dict(message or {})
    safe_message.setdefault("provider", "brevo")
    safe_message.setdefault("event_type", None)
    safe_message.setdefault("message_id", None)
    safe_message.setdefault("conversation_id", None)
    safe_message.setdefault("sender_phone", None)
    safe_message.setdefault("sender_name", None)
    safe_message.setdefault("text", None)
    safe_message["raw"] = to_jsonb(safe_message.get("raw") or {})

    if not safe_message.get("message_id"):
        return True, insert_inbound_message(message)

    ensure_tables()
    lock_key = f"{safe_message['provider']}:{safe_message['message_id']}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": lock_key},
            )
            cur.execute(
                """
                SELECT id
                FROM public.ai_inbound_messages
                WHERE provider = %(provider)s AND message_id = %(message_id)s
                LIMIT 1
                """,
                safe_message,
            )
            existing = cur.fetchone()
            if existing:
                return False, get_returning_id(existing)

            cur.execute(
                """
                INSERT INTO public.ai_inbound_messages
                  (provider, event_type, message_id, conversation_id, sender_phone, sender_name, text, raw)
                VALUES
                  (%(provider)s, %(event_type)s, %(message_id)s, %(conversation_id)s,
                   %(sender_phone)s, %(sender_name)s, %(text)s, %(raw)s)
                RETURNING id
                """,
                safe_message,
            )
            return True, get_returning_id(cur.fetchone())


def is_latest_inbound_message(
    inbound_id: int | None,
    conversation_id: str | None,
    sender_phone: str | None,
) -> bool:
    """Check whether no later inbound row exists for this conversation/contact."""
    settings = get_settings()
    if not settings.database_url or not inbound_id:
        return True
    if not conversation_id and not sender_phone:
        return True

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if conversation_id:
                cur.execute(
                    """
                    SELECT 1 FROM public.ai_inbound_messages
                    WHERE id > %(inbound_id)s AND conversation_id = %(conversation_id)s
                    LIMIT 1
                    """,
                    {"inbound_id": inbound_id, "conversation_id": conversation_id},
                )
            else:
                cur.execute(
                    """
                    SELECT 1 FROM public.ai_inbound_messages
                    WHERE id > %(inbound_id)s AND sender_phone = %(sender_phone)s
                    LIMIT 1
                    """,
                    {"inbound_id": inbound_id, "sender_phone": sender_phone},
                )
            return cur.fetchone() is None


def insert_agent_response(data: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_data = dict(data or {})

    safe_data.setdefault("inbound_id", None)
    safe_data.setdefault("sender_phone", None)
    safe_data.setdefault("reply_text", "")
    safe_data.setdefault("intent", None)
    safe_data.setdefault("handoff_required", False)
    safe_data.setdefault("safety_reason", None)
    safe_data.setdefault("provider_send_ok", False)

    safe_data["provider_response"] = to_jsonb(safe_data.get("provider_response") or {})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_agent_responses
                  (
                    inbound_id,
                    sender_phone,
                    reply_text,
                    intent,
                    handoff_required,
                    safety_reason,
                    provider_send_ok,
                    provider_response
                  )
                VALUES
                  (
                    %(inbound_id)s,
                    %(sender_phone)s,
                    %(reply_text)s,
                    %(intent)s,
                    %(handoff_required)s,
                    %(safety_reason)s,
                    %(provider_send_ok)s,
                    %(provider_response)s
                  )
                RETURNING id
                """,
                safe_data,
            )

            row = cur.fetchone()
            return get_returning_id(row)
