from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .config import get_settings
from .runtime_context import register_database_call


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
    register_database_call()
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
    if not settings.database_url or not getattr(settings, "auto_create_tables", False):
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
                  channel text NOT NULL DEFAULT 'unknown',
                  sender_key text NULL,
                  sender_external_id text NULL,
                  visitor_id text NULL,
                  sender_username text NULL,
                  source_channel_ref text NULL,
                  source_channel_link text NULL,
                  source_conversation_ref text NULL,
                  sender_phone text NULL,
                  sender_name text NULL,
                  text text NOT NULL DEFAULT '',
                  channel_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                ALTER TABLE public.ai_inbound_messages
                  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
                  ADD COLUMN IF NOT EXISTS sender_key text,
                  ADD COLUMN IF NOT EXISTS sender_external_id text,
                  ADD COLUMN IF NOT EXISTS visitor_id text,
                  ADD COLUMN IF NOT EXISTS sender_username text,
                  ADD COLUMN IF NOT EXISTS source_channel_ref text,
                  ADD COLUMN IF NOT EXISTS source_channel_link text,
                  ADD COLUMN IF NOT EXISTS source_conversation_ref text,
                  ADD COLUMN IF NOT EXISTS channel_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_sender_phone
                ON public.ai_inbound_messages(sender_phone);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_created_at
                ON public.ai_inbound_messages(created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_sender_key_created_at
                ON public.ai_inbound_messages(sender_key, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_channel_created_at
                ON public.ai_inbound_messages(channel, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_visitor_id
                ON public.ai_inbound_messages(visitor_id);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_source_conversation_ref
                ON public.ai_inbound_messages(channel, source_conversation_ref);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_conversation_created_at
                ON public.ai_inbound_messages(conversation_id, created_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_inbound_provider_message_id
                ON public.ai_inbound_messages(provider, message_id)
                WHERE message_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS public.ai_agent_responses (
                  id bigserial PRIMARY KEY,
                  inbound_id bigint NULL REFERENCES public.ai_inbound_messages(id) ON DELETE SET NULL,
                  channel text NOT NULL DEFAULT 'unknown',
                  sender_key text NULL,
                  sender_phone text NULL,
                  reply_text text NOT NULL,
                  intent text NULL,
                  handoff_required boolean NOT NULL DEFAULT false,
                  safety_reason text NULL,
                  provider_send_ok boolean NOT NULL DEFAULT false,
                  provider_response jsonb NULL,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                ALTER TABLE public.ai_agent_responses
                  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
                  ADD COLUMN IF NOT EXISTS sender_key text;

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_inbound_id
                ON public.ai_agent_responses(inbound_id);

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_created_at
                ON public.ai_agent_responses(created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_sender_key_created_at
                ON public.ai_agent_responses(sender_key, created_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_customer_identity_links (
                  id bigserial PRIMARY KEY,
                  person_key text NOT NULL,
                  identity_type text NOT NULL,
                  identity_value text NOT NULL,
                  channel text NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  CONSTRAINT uq_ai_customer_identity_type_value
                    UNIQUE (identity_type, identity_value)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_person_key
                ON public.ai_customer_identity_links(person_key);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_value
                ON public.ai_customer_identity_links(identity_type, identity_value);

                CREATE TABLE IF NOT EXISTS public.ai_customer_commerce_sessions (
                  person_key text PRIMARY KEY,
                  commerce_state jsonb NOT NULL DEFAULT '{}'::jsonb,
                  channel text NULL,
                  conversation_id text NULL,
                  sender_key text NULL,
                  sender_phone text NULL,
                  resumable_score integer NOT NULL DEFAULT 0,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_updated_at
                ON public.ai_customer_commerce_sessions(updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_key
                ON public.ai_customer_commerce_sessions(sender_key);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_phone
                ON public.ai_customer_commerce_sessions(sender_phone);
                """
            )


def _prepare_inbound_message(message: dict[str, Any]) -> dict[str, Any]:
    safe_message = dict(message or {})
    defaults = {
        "provider": "brevo",
        "event_type": None,
        "message_id": None,
        "conversation_id": None,
        "channel": "unknown",
        "sender_key": None,
        "sender_external_id": None,
        "visitor_id": None,
        "sender_username": None,
        "source_channel_ref": None,
        "source_channel_link": None,
        "source_conversation_ref": None,
        "sender_phone": None,
        "sender_name": None,
        "text": "",
    }
    for key, value in defaults.items():
        safe_message.setdefault(key, value)
    safe_message["channel_metadata"] = to_jsonb(safe_message.get("channel_metadata") or {})
    safe_message["raw"] = to_jsonb(safe_message.get("raw") or {})
    return safe_message


def resolve_context_filter(
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
    *,
    table_alias: str = "inbound",
) -> tuple[str | None, dict[str, Any]]:
    if conversation_id:
        return (
            f"{table_alias}.conversation_id = %(conversation_id)s",
            {"conversation_id": conversation_id},
        )
    if sender_key:
        return (
            f"{table_alias}.sender_key = %(sender_key)s",
            {"sender_key": sender_key},
        )
    if sender_phone:
        return (
            f"{table_alias}.sender_phone = %(sender_phone)s",
            {"sender_phone": sender_phone},
        )
    return None, {}


def insert_inbound_message(message: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_message = _prepare_inbound_message(message)

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
                    channel,
                    sender_key,
                    sender_external_id,
                    visitor_id,
                    sender_username,
                    source_channel_ref,
                    source_channel_link,
                    source_conversation_ref,
                    sender_phone,
                    sender_name,
                    text,
                    channel_metadata,
                    raw
                  )
                VALUES
                  (
                    %(provider)s,
                    %(event_type)s,
                    %(message_id)s,
                    %(conversation_id)s,
                    %(channel)s,
                    %(sender_key)s,
                    %(sender_external_id)s,
                    %(visitor_id)s,
                    %(sender_username)s,
                    %(source_channel_ref)s,
                    %(source_channel_link)s,
                    %(source_conversation_ref)s,
                    %(sender_phone)s,
                    %(sender_name)s,
                    %(text)s,
                    %(channel_metadata)s,
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

    safe_message = _prepare_inbound_message(message)

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
                  (
                    provider, event_type, message_id, conversation_id, channel,
                    sender_key, sender_external_id, visitor_id, sender_username,
                    source_channel_ref, source_channel_link, source_conversation_ref,
                    sender_phone, sender_name, text, channel_metadata, raw
                  )
                VALUES
                  (
                    %(provider)s, %(event_type)s, %(message_id)s, %(conversation_id)s,
                    %(channel)s, %(sender_key)s, %(sender_external_id)s, %(visitor_id)s,
                    %(sender_username)s, %(source_channel_ref)s, %(source_channel_link)s,
                    %(source_conversation_ref)s, %(sender_phone)s, %(sender_name)s,
                    %(text)s, %(channel_metadata)s, %(raw)s
                  )
                RETURNING id
                """,
                safe_message,
            )
            return True, get_returning_id(cur.fetchone())


def is_latest_inbound_message(
    inbound_id: int | None,
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
) -> bool:
    """Check whether no later inbound row exists for this conversation/contact."""
    settings = get_settings()
    if not settings.database_url or not inbound_id:
        return True
    conversation_filter, params = resolve_context_filter(
        conversation_id,
        sender_key,
        sender_phone,
    )
    if not conversation_filter:
        return True

    ensure_tables()
    params["inbound_id"] = inbound_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1
                FROM public.ai_inbound_messages AS inbound
                WHERE inbound.id > %(inbound_id)s
                  AND {conversation_filter}
                LIMIT 1
                """,
                params,
            )
            return cur.fetchone() is None


def _history_identity_candidates(
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
) -> list[tuple[str | None, str | None, str | None]]:
    candidates: list[tuple[str | None, str | None, str | None]] = [
        (conversation_id, sender_key, sender_phone),
    ]
    if conversation_id and (sender_key or sender_phone):
        candidates.append((None, sender_key, sender_phone))
    if sender_key:
        candidates.append((None, sender_key, None))
    if sender_phone:
        candidates.append((None, None, sender_phone))
    try:
        from .customer_identity import resolve_linked_identity_candidates

        for linked_key, linked_phone in resolve_linked_identity_candidates(
            sender_key=sender_key,
            sender_phone=sender_phone,
        ):
            candidates.append((None, linked_key, linked_phone))
    except Exception as exc:
        print("[sales.context] identity_resolve_failed", {
            "error_type": type(exc).__name__,
        })
    return candidates


def load_recent_conversation_turns(
    *,
    conversation_id: str | None,
    sender_phone: str | None,
    before_inbound_id: int | None,
    limit: int = 8,
    sender_key: str | None = None,
) -> list[dict[str, Any]]:
    """Load a small, chronological transcript containing only delivered replies."""
    settings = get_settings()
    if not settings.database_url:
        return []

    safe_limit = max(1, min(int(limit), 8))
    before_filter = (
        "AND inbound.id < %(before_inbound_id)s"
        if before_inbound_id is not None
        else ""
    )
    seen_filters: set[str] = set()
    rows_by_id: dict[int, dict[str, Any]] = {}
    try:
        for conv, key, phone in _history_identity_candidates(
            conversation_id,
            sender_key,
            sender_phone,
        ):
            conversation_filter, identity_params = resolve_context_filter(
                conv,
                key,
                phone,
            )
            if not conversation_filter:
                continue
            filter_token = f"{conversation_filter}|{sorted(identity_params.items())}"
            if filter_token in seen_filters:
                continue
            seen_filters.add(filter_token)
            params: dict[str, Any] = {
                "before_inbound_id": before_inbound_id,
                "limit": safe_limit,
            }
            params.update(identity_params)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT inbound.id, inbound.text, delivered.reply_text, delivered.safety_reason
                        FROM public.ai_inbound_messages AS inbound
                        LEFT JOIN LATERAL (
                            SELECT response.reply_text, response.safety_reason
                            FROM public.ai_agent_responses AS response
                            WHERE response.inbound_id = inbound.id
                              AND response.provider_send_ok = true
                            ORDER BY response.id DESC
                            LIMIT 1
                        ) AS delivered ON true
                        WHERE {conversation_filter}
                          {before_filter}
                        ORDER BY inbound.id DESC
                        LIMIT %(limit)s
                        """,
                        params,
                    )
                    for row in cur.fetchall() or []:
                        inbound_id = row.get("id")
                        if inbound_id is None:
                            continue
                        rows_by_id[int(inbound_id)] = row
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.context] load_failed", {"error_type": type(exc).__name__})
        return []

    rows = [rows_by_id[key] for key in sorted(rows_by_id)][-safe_limit:]
    turns: list[dict[str, Any]] = []
    for row in rows:
        inbound_text = str(row.get("text") or "").strip()
        reply_text = str(row.get("reply_text") or "").strip()
        if inbound_text:
            turns.append({"role": "user", "content": inbound_text})
        if reply_text:
            assistant_turn: dict[str, Any] = {"role": "assistant", "content": reply_text}
            if row.get("safety_reason"):
                assistant_turn["metadata"] = {"safety_reason": str(row["safety_reason"])}
            turns.append(assistant_turn)
    return turns[-safe_limit:]


def _commerce_state_from_provider_response(provider_response: Any) -> dict[str, Any]:
    agent_context = (
        provider_response.get("_agent_context")
        if isinstance(provider_response, dict)
        else None
    )
    state = agent_context.get("commerce_state") if isinstance(agent_context, dict) else None
    return state if isinstance(state, dict) else {}


def _load_commerce_states_for_filter(
    *,
    conversation_filter: str,
    identity_params: dict[str, Any],
    before_inbound_id: int | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "before_inbound_id": before_inbound_id,
        "limit": max(1, min(int(limit), 20)),
    }
    params.update(identity_params)
    before_filter = (
        "AND inbound.id < %(before_inbound_id)s"
        if before_inbound_id is not None
        else ""
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT response.provider_response
                FROM public.ai_agent_responses AS response
                JOIN public.ai_inbound_messages AS inbound
                  ON inbound.id = response.inbound_id
                WHERE {conversation_filter}
                  {before_filter}
                  AND response.provider_send_ok = true
                  AND response.provider_response ? '_agent_context'
                ORDER BY response.id DESC
                LIMIT %(limit)s
                """,
                params,
            )
            rows = cur.fetchall() or []
    states: list[dict[str, Any]] = []
    for row in rows:
        provider_response = row.get("provider_response") if isinstance(row, dict) else None
        state = _commerce_state_from_provider_response(provider_response)
        if state:
            states.append(state)
    return states


def load_customer_commerce_sessions(
    person_keys: list[str],
) -> list[dict[str, Any]]:
    """Load durable commerce sessions for one or more person_key aliases."""
    settings = get_settings()
    keys = [str(key).strip() for key in person_keys if str(key or "").strip()]
    if not settings.database_url or not keys:
        return []
    ensure_tables()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT person_key, commerce_state, resumable_score, updated_at
                    FROM public.ai_customer_commerce_sessions
                    WHERE person_key = ANY(%(keys)s)
                    ORDER BY resumable_score DESC, updated_at DESC
                    """,
                    {"keys": keys},
                )
                rows = cur.fetchall() or []
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.session] load_failed", {"error_type": type(exc).__name__})
        return []

    sessions: list[dict[str, Any]] = []
    for row in rows:
        state = row.get("commerce_state") if isinstance(row, dict) else None
        if isinstance(state, dict) and state:
            sessions.append(state)
    return sessions


def persist_customer_commerce_session(
    *,
    person_keys: list[str],
    commerce_state: dict[str, Any],
    channel: str | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
) -> None:
    """Persist durable working memory under all known person_key aliases."""
    from .context_resume import (
        commerce_state_resumable_score,
        merge_commerce_states,
    )

    settings = get_settings()
    keys = [str(key).strip() for key in person_keys if str(key or "").strip()]
    if not settings.database_url or not keys or not isinstance(commerce_state, dict):
        return

    ensure_tables()
    try:
        existing_sessions = load_customer_commerce_sessions(keys)
        donor = (
            max(existing_sessions, key=commerce_state_resumable_score)
            if existing_sessions
            else {}
        )
        merged = merge_commerce_states(commerce_state, donor)
        score = commerce_state_resumable_score(merged)
        if score <= 0:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
                for key in keys:
                    cur.execute(
                        """
                        INSERT INTO public.ai_customer_commerce_sessions (
                          person_key,
                          commerce_state,
                          channel,
                          conversation_id,
                          sender_key,
                          sender_phone,
                          resumable_score,
                          updated_at
                        )
                        VALUES (
                          %(person_key)s,
                          %(commerce_state)s,
                          %(channel)s,
                          %(conversation_id)s,
                          %(sender_key)s,
                          %(sender_phone)s,
                          %(resumable_score)s,
                          now()
                        )
                        ON CONFLICT (person_key) DO UPDATE
                        SET
                          commerce_state = EXCLUDED.commerce_state,
                          channel = COALESCE(
                            EXCLUDED.channel,
                            public.ai_customer_commerce_sessions.channel
                          ),
                          conversation_id = COALESCE(
                            EXCLUDED.conversation_id,
                            public.ai_customer_commerce_sessions.conversation_id
                          ),
                          sender_key = COALESCE(
                            EXCLUDED.sender_key,
                            public.ai_customer_commerce_sessions.sender_key
                          ),
                          sender_phone = COALESCE(
                            EXCLUDED.sender_phone,
                            public.ai_customer_commerce_sessions.sender_phone
                          ),
                          resumable_score = EXCLUDED.resumable_score,
                          updated_at = now()
                        """,
                        {
                            "person_key": key,
                            "commerce_state": to_jsonb(merged),
                            "channel": channel,
                            "conversation_id": conversation_id,
                            "sender_key": sender_key,
                            "sender_phone": sender_phone,
                            "resumable_score": score,
                        },
                    )
        print("[sales.session] upserted", {
            "aliases": len(keys),
            "person_key_prefix": keys[0].split(":", 1)[0],
            "resumable_score": score,
            "has_order": bool(merged.get("order_id")),
        })
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.session] upsert_failed", {"error_type": type(exc).__name__})


def load_commerce_conversation_state(
    *,
    conversation_id: str | None,
    sender_phone: str | None,
    before_inbound_id: int | None,
    sender_key: str | None = None,
) -> dict[str, Any]:
    """Load delivered commerce state, recovering order context across identities."""
    from .context_resume import (
        commerce_state_resumable_score,
        merge_commerce_states,
    )
    from .customer_identity import resolve_person_key_candidates

    settings = get_settings()
    if not settings.database_url:
        return {}

    identity_candidates: list[tuple[str | None, str | None, str | None]] = [
        (conversation_id, sender_key, sender_phone),
    ]
    # Fallback across channel identities when conversation_id is sparse/stale.
    if conversation_id and (sender_key or sender_phone):
        identity_candidates.append((None, sender_key, sender_phone))
    if sender_key:
        identity_candidates.append((None, sender_key, None))
    if sender_phone:
        identity_candidates.append((None, None, sender_phone))
    try:
        from .customer_identity import resolve_linked_identity_candidates

        for linked_key, linked_phone in resolve_linked_identity_candidates(
            sender_key=sender_key,
            sender_phone=sender_phone,
        ):
            identity_candidates.append((None, linked_key, linked_phone))
    except Exception as exc:
        print("[sales.context.state] identity_resolve_failed", {
            "error_type": type(exc).__name__,
        })

    seen_filters: set[str] = set()
    collected: list[dict[str, Any]] = []
    try:
        for conv, key, phone in identity_candidates:
            conversation_filter, identity_params = resolve_context_filter(
                conv,
                key,
                phone,
            )
            if not conversation_filter:
                continue
            filter_token = f"{conversation_filter}|{sorted(identity_params.items())}"
            if filter_token in seen_filters:
                continue
            seen_filters.add(filter_token)
            collected.extend(
                _load_commerce_states_for_filter(
                    conversation_filter=conversation_filter,
                    identity_params=identity_params,
                    before_inbound_id=before_inbound_id,
                )
            )
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.context.state] load_failed", {"error_type": type(exc).__name__})
        collected = []

    merged: dict[str, Any] = {}
    if collected:
        latest = collected[0]
        richest = max(collected, key=commerce_state_resumable_score)
        merged = merge_commerce_states(latest, richest)

    person_keys = resolve_person_key_candidates(
        sender_key=sender_key,
        sender_phone=sender_phone,
        state=merged or None,
    )
    durable_sessions = load_customer_commerce_sessions(person_keys)
    durable = (
        max(durable_sessions, key=commerce_state_resumable_score)
        if durable_sessions
        else {}
    )
    if durable:
        merged = merge_commerce_states(merged, durable)

    print("[sales.context.state] loaded", {
        "candidates": len(collected),
        "durable_sessions": len(durable_sessions),
        "merged_has_order": bool(merged.get("order_id")),
        "merged_pending_action": merged.get("pending_action"),
        "merged_score": commerce_state_resumable_score(merged),
    })
    return merged


def has_successful_agent_response(inbound_id: int | None) -> bool:
    """Return True when a successful outbound reply already exists for inbound_id."""
    settings = get_settings()
    if not settings.database_url or inbound_id is None:
        return False

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.ai_agent_responses
                WHERE inbound_id = %(inbound_id)s
                  AND provider_send_ok = true
                LIMIT 1
                """,
                {"inbound_id": inbound_id},
            )
            return cur.fetchone() is not None


def insert_agent_response(data: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_data = dict(data or {})

    safe_data.setdefault("inbound_id", None)
    safe_data.setdefault("channel", "unknown")
    safe_data.setdefault("sender_key", None)
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
                    channel,
                    sender_key,
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
                    %(channel)s,
                    %(sender_key)s,
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
