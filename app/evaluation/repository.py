from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from app.db import get_conn, to_jsonb


def historical_cases(workspace_id: str, *, limit: int, history_turns: int, lookback_days: int) -> list[dict]:
    """One canonical delivered answer per input, with only its preceding context.

    Conversation, channel and provider account are all part of the boundary.
    Undelivered attempts never become the expected answer or prior assistant turn.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT i.id AS inbound_id, i.conversation_id, i.channel,
                   coalesce(i.source_channel_ref,'') AS account,
                   i.text AS customer_text, r.id AS response_id, r.reply_text,
                   r.intent, r.handoff_required, r.safety_reason, i.created_at AS recorded_at,
                   coalesce(r.provider_response->'_agent_metadata',r.provider_response->'_agent_context','{}'::jsonb) AS metadata
            FROM public.ai_inbound_messages i
            JOIN LATERAL (
                SELECT r.* FROM public.ai_agent_responses r
                WHERE r.inbound_id=i.id AND (r.workspace_id IS NULL OR r.workspace_id=i.workspace_id)
                  AND r.provider_send_ok IS TRUE AND nullif(trim(r.reply_text),'') IS NOT NULL
                  AND lower(coalesce(r.provider_response->>'skipped','false')) NOT IN ('true','1')
                  AND lower(coalesce(r.provider_response->>'dry_run','false')) NOT IN ('true','1')
                ORDER BY r.id DESC LIMIT 1
            ) r ON true
            WHERE i.workspace_id=%s::uuid AND nullif(i.conversation_id,'') IS NOT NULL
              AND nullif(trim(i.text),'') IS NOT NULL
              AND i.created_at >= now() - make_interval(days => %s)
            ORDER BY i.id DESC LIMIT %s
        """, (workspace_id, lookback_days, min(2000, limit * (history_turns + 1))))
        rows = list(reversed([dict(row) for row in cur.fetchall()]))
    return cases_from_rows(rows, workspace_id, limit=limit, history_turns=history_turns)


def cases_from_rows(rows, workspace_id, *, limit, history_turns):
    histories: dict[tuple, list] = {}
    cases = []
    for row in rows:
        boundary = (row['conversation_id'], row['channel'], row.get('account') or '')
        previous = histories.setdefault(boundary, [])
        case = {
            'workspace_id': workspace_id, 'source_response_id': row['response_id'],
            'source_inbound_id': row['inbound_id'], 'channel': row['channel'],
            'input': row['customer_text'], 'historical_reply': row['reply_text'],
            'recorded_at': str(row['recorded_at']) if row.get('recorded_at') else None,
            'historical_metadata': row.get('metadata') or {},
            'historical_handoff': bool(row.get('handoff_required')),
            'historical_safety_reason': row.get('safety_reason'),
            'history': [{'role': p['role'], 'content': p['content']} for p in previous[-history_turns * 2:]],
            'initial_state': deepcopy(previous[-1].get('state', {})) if previous else {},
            'context_complete': False,
            'context_source': 'recorded_window',
        }
        case['fingerprint'] = hashlib.sha256(json.dumps(case, sort_keys=True, default=str).encode()).hexdigest()
        cases.append(case)
        previous.extend([{'role': 'user', 'content': row['customer_text']},
                         {'role': 'assistant', 'content': row['reply_text'],
                          'state': (row.get('metadata') or {}).get('commerce_state') or {}}])
    # Alternate failures and ordinary answers so quality comparisons include successes.
    failed = [c for c in reversed(cases) if c['historical_handoff'] or c['historical_safety_reason']]
    ordinary = [c for c in reversed(cases) if not c['historical_handoff'] and not c['historical_safety_reason']]
    selected = []
    while (failed or ordinary) and len(selected) < limit:
        for bucket in (failed, ordinary):
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    return selected


def import_cases(workspace_id, cases):
    imported = []
    with get_conn() as conn, conn.cursor() as cur:
        for case in cases:
            cur.execute("""INSERT INTO public.ai_conversation_evaluation_cases
                (workspace_id,source_response_id,payload) VALUES (%s::uuid,%s,%s)
                ON CONFLICT (workspace_id,source_response_id) DO UPDATE
                SET payload=EXCLUDED.payload,updated_at=now()
                RETURNING id,source_response_id
            """, (workspace_id, case['source_response_id'], to_jsonb(case)))
            imported.append(dict(cur.fetchone()))
    return imported


def get_case(workspace_id, case_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM public.ai_conversation_evaluation_cases WHERE id=%s::uuid AND workspace_id=%s::uuid",
                    (case_id, workspace_id))
        row = cur.fetchone()
        if not row:
            raise ValueError('evaluation_case_not_found')
        return row['payload']


def start_run(workspace_id, case_id, request_id, versions):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO public.ai_conversation_evaluation_runs(id,workspace_id,case_id,versions,status)
            VALUES (%s::uuid,%s::uuid,%s::uuid,%s,'running') ON CONFLICT (id) DO NOTHING RETURNING id
        """, (request_id, workspace_id, case_id, to_jsonb(versions)))
        return cur.fetchone() is not None


def finish_run(workspace_id, run_id, status, result):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE public.ai_conversation_evaluation_runs SET status=%s,result=%s,finished_at=now()
            WHERE id=%s::uuid AND workspace_id=%s::uuid AND status='running'
        """, (status, to_jsonb(result), run_id, workspace_id))


def list_results(workspace_id, limit=50):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id,source_response_id,payload->>'input' AS input,updated_at
            FROM public.ai_conversation_evaluation_cases WHERE workspace_id=%s::uuid ORDER BY updated_at DESC LIMIT %s
        """, (workspace_id, limit))
        cases = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT id,case_id,status,versions,result,created_at,finished_at
            FROM public.ai_conversation_evaluation_runs WHERE workspace_id=%s::uuid ORDER BY created_at DESC LIMIT %s
        """, (workspace_id, limit))
        return {'cases': cases, 'runs': [dict(row) for row in cur.fetchall()]}
