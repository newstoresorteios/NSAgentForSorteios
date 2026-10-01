"""Durable single-flight jobs. PostgreSQL owns leases/generations; Redis is a cache.

No customer text, price, stock, credentials or signed media URLs enter this cache.
Reading the current DB generation before Redis prevents an invalidated result
from being resurrected by a late worker or by another application instance.
"""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

from app.config import get_settings
from app.db import get_conn, to_jsonb
from app.ops.observability import log_event

EVIDENCE_VERSION = 'story-worker-v1'
META = 'id, workspace_id, tenant_id, provider, instagram_account_id, story_media_id, analysis_version, generation, status, source_inbound_id, media_storage_path, media_sha256, media_mime, media_bytes, attempts, lease_owner, expires_at, last_error'


def analysis_version():
    s = get_settings()
    controls = [EVIDENCE_VERSION, s.instagram_story_analysis_version,
                s.instagram_story_vision_model or s.openai_main_model or s.openai_model,
                s.openai_transcribe_model, s.instagram_story_video_max_frames,
                s.instagram_story_video_audio_enabled, s.instagram_story_video_frame_analysis_enabled,
                s.instagram_story_video_max_seconds, s.instagram_story_analysis_detail]
    return hashlib.sha256(json.dumps(controls).encode()).hexdigest()[:24]


def ensure_job(*, workspace_id, tenant_id, story, inbound_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO public.instagram_story_analysis_jobs
                  (workspace_id, tenant_id, provider, instagram_account_id, story_media_id,
                   analysis_version, source_inbound_id)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, tenant_id, provider, instagram_account_id, story_media_id, analysis_version)
                DO UPDATE SET source_inbound_id = EXCLUDED.source_inbound_id,
                  generation = CASE WHEN instagram_story_analysis_jobs.expires_at < now() THEN gen_random_uuid() ELSE instagram_story_analysis_jobs.generation END,
                  status = CASE WHEN instagram_story_analysis_jobs.expires_at < now() THEN 'pending' ELSE instagram_story_analysis_jobs.status END,
                  result = CASE WHEN instagram_story_analysis_jobs.expires_at < now() THEN '{{}}'::jsonb ELSE instagram_story_analysis_jobs.result END,
                  attempts = CASE WHEN instagram_story_analysis_jobs.expires_at < now() THEN 0 ELSE instagram_story_analysis_jobs.attempts END,
                  expires_at = CASE WHEN instagram_story_analysis_jobs.expires_at < now() THEN now() + interval '7 days' ELSE instagram_story_analysis_jobs.expires_at END,
                  updated_at = now()
                RETURNING {META}
            """, (workspace_id, tenant_id, story.provider, story.instagram_account_id,
                  story.story_media_id, analysis_version(), inbound_id))
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def claim_job():
    owner = str(uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            # A process killed during its last attempt must eventually release waiters.
            cur.execute("""UPDATE public.instagram_story_analysis_jobs
                SET status='failed', last_error='worker_attempts_exhausted', lease_owner=NULL,
                    expires_at=now()+interval '2 minutes'
                WHERE status='processing' AND lease_until < now() AND attempts >= 3""")
            cur.execute(f"""WITH next AS (
                  SELECT id FROM public.instagram_story_analysis_jobs
                  WHERE attempts < 3 AND available_at <= now()
                    AND (status='pending' OR (status='processing' AND lease_until < now()))
                  ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE public.instagram_story_analysis_jobs j
                  SET status='processing', lease_owner=%s::uuid,
                      lease_until=now()+interval '240 seconds', attempts=attempts+1, updated_at=now()
                  FROM next WHERE j.id=next.id RETURNING j.*""", (owner,))
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def finish_job(job, *, result=None, error=None, media=None):
    terminal = bool(error and (job['attempts'] >= 3 or error in {'file_too_large', 'duration_limit', 'http_404', 'http_410'}))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.instagram_story_analysis_jobs
                SET status=%s, result=%s::jsonb, last_error=%s,
                    lease_owner=NULL, lease_until=NULL, updated_at=now(),
                    available_at=now()+interval '15 seconds',
                    expires_at=now() + CASE WHEN %s THEN interval '2 minutes' ELSE interval '7 days' END,
                    media_storage_path=COALESCE(%s, media_storage_path),
                    media_sha256=COALESCE(%s, media_sha256), media_mime=COALESCE(%s, media_mime),
                    media_bytes=COALESCE(%s, media_bytes)
                WHERE id=%s AND generation=%s::uuid AND lease_owner=%s::uuid
                  AND status='processing' AND lease_until > now() RETURNING id
            """, ('failed' if terminal else 'pending' if error else 'ready', to_jsonb(result or {}),
                  error, terminal, (media or {}).get('storage_path'), (media or {}).get('sha256'),
                  (media or {}).get('mime'), (media or {}).get('byte_count'),
                  job['id'], str(job['generation']), str(job['lease_owner'])))
            saved = bool(cur.fetchone())
        conn.commit()
    return saved


def source_media(job):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, workspace_id, channel_metadata,
                    raw #> '{meta_event,message,reply_to,story}' AS source_story
                FROM public.ai_inbound_messages
                WHERE id=%s AND workspace_id=%s::uuid AND channel='instagram'""",
                (job['source_inbound_id'], str(job['workspace_id'])))
            return cur.fetchone()


def cache_key(job):
    scope = [str(job[k]) for k in ('workspace_id', 'tenant_id', 'provider', 'instagram_account_id',
                                  'story_media_id', 'analysis_version', 'generation')]
    return 'story:evidence:v1:' + hashlib.sha256(json.dumps(scope).encode()).hexdigest()


async def redis_command(command, job, payload=None):
    url = os.getenv('STORY_REDIS_URL') or os.getenv('REDIS_URL') or os.getenv('REDIS_TLS_URL')
    if not url:
        return None
    try:
        import redis.asyncio as redis
        # Scope a pool to the invocation: serverless runtimes can replace event loops.
        async with redis.from_url(url, socket_connect_timeout=1, socket_timeout=1,
                                  decode_responses=True, max_connections=2) as client:
            if command == 'get':
                value = await client.get(cache_key(job))
                return json.loads(value) if value else None
            await client.set(cache_key(job), json.dumps(payload, ensure_ascii=False), ex=86400)
    except Exception as exc:
        log_event('story.redis_unavailable', {'error_type': type(exc).__name__})
    return None


async def read_result(job):
    if job['status'] != 'ready':
        return None
    cached = await redis_command('get', job)
    if isinstance(cached, dict) and cached.get('schema') == EVIDENCE_VERSION:
        return cached
    import asyncio
    def read():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT result FROM public.instagram_story_analysis_jobs
                    WHERE id=%s AND generation=%s::uuid AND status='ready' AND expires_at > now()""",
                    (job['id'], str(job['generation'])))
                row = cur.fetchone()
                return row['result'] if row else None
    result = await asyncio.to_thread(read)
    if result:
        await redis_command('set', job, result)
    return result


def defer_inbox(inbox_ids, job_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.ai_inbound_inbox
                SET status='pending', attempts=GREATEST(attempts-1, 0), story_analysis_job_id=%s,
                    lease_owner=NULL, lease_expires_at=NULL, last_error='story_analysis_pending', updated_at=now()
                WHERE id=ANY(%s) AND status='leased'""", (job_id, inbox_ids))
        conn.commit()
