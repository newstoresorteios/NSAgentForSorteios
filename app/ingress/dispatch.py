"""Immediate drain after durable acceptance; cron recovers interrupted requests."""
from __future__ import annotations
import asyncio
import hmac
import re
import time
from functools import lru_cache
from fastapi import Header, HTTPException


@lru_cache(maxsize=1)
def _dispatch_secret(time_bucket: int) -> str:
    from app.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = %s", ("nsagent_queue_dispatch",))
            row = cur.fetchone()
    if not row or not row.get("decrypted_secret"):
        raise RuntimeError("queue_dispatch_secret_missing")
    return row["decrypted_secret"]


def verify_queue_dispatch(authorization: str | None = Header(default=None)):
    if not authorization or not re.fullmatch(r"Bearer [a-f0-9]{64}", authorization):
        raise HTTPException(status_code=401, detail="invalid_queue_dispatch_token")
    try:
        expected = _dispatch_secret(int(time.time() // 60))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="queue_dispatch_unavailable") from exc
    if not hmac.compare_digest(authorization[7:], expected):
        raise HTTPException(status_code=401, detail="invalid_queue_dispatch_token")


async def dispatch_pending_queues() -> None:
    from app.ingress.worker import process_inbox_batch
    from app.ingress.outbox_worker import process_outbox_batch
    from app.ops.observability import log_exception
    from app.config import get_settings
    from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
    settings = get_settings()
    tokens = None
    if settings.database_url and settings.agent_db_persona_enabled:
        from app.persona.persona_runtime import load_persona_runtime
        runtime = await asyncio.to_thread(load_persona_runtime)
        if runtime.configuration_bundle:
            settings = settings_from_bundle(settings, runtime.configuration_bundle)
            tokens = bind_bundle(runtime.configuration_bundle, settings)
    # Each queue already owns leases/idempotency. Bound the work attached to a
    # webhook; durable retries remain available if the process stops.
    try:
        results = await asyncio.gather(
            process_inbox_batch(limit=settings.agent_inbox_batch_size),
            process_outbox_batch(limit=settings.agent_inbox_batch_size), return_exceptions=True)
    finally:
        if tokens is not None:
            reset_bundle(tokens)
    for name, result in zip(("inbox", "outbox"), results):
        if isinstance(result, Exception):
            log_exception("queue.immediate_dispatch_failed", result, {"queue": name})
