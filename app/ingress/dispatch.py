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
    from app.stories.story_analysis_worker import process_story_analysis_batch
    from app.ops.observability import log_exception
    from app.config import get_settings
    settings = get_settings()

    # Do not resolve a global persona/configuration bundle here. In a
    # multi-workspace database there can be several active personas, so a
    # workspace-less lookup is intentionally ambiguous. Inbox processing
    # resolves and binds the runtime after each row's workspace is known;
    # outbox delivery does not need persona configuration.
    # Each queue already owns leases/idempotency. Bound the work attached to a
    # webhook; durable retries remain available if the process stops.
    results = await asyncio.gather(
        process_inbox_batch(limit=settings.agent_inbox_batch_size),
        process_outbox_batch(limit=settings.agent_inbox_batch_size),
        return_exceptions=True,
    )
    for name, result in zip(("inbox", "outbox"), results):
        if isinstance(result, Exception):
            log_exception("queue.immediate_dispatch_failed", result, {"queue": name})
    # New Story jobs have just been enqueued by inbox preflight. The same durable
    # dispatcher is also called by pg_cron after interrupted serverless requests.
    try:
        stories = await process_story_analysis_batch(limit=1)
        if stories['claimed']:
            await process_inbox_batch(limit=settings.agent_inbox_batch_size)
    except Exception as exc:
        log_exception('queue.immediate_dispatch_failed', exc, {'queue': 'stories'})
