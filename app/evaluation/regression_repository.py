from __future__ import annotations

import hashlib
import json
from app.db import get_conn, to_jsonb


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def register_suite(workspace, specification):
    digest = fingerprint(specification)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO public.ai_regression_suites(workspace_id,name,version,fingerprint,specification)
            VALUES (%s::uuid,%s,%s,%s,%s) ON CONFLICT(workspace_id,name,version) DO NOTHING''',
            (workspace,specification['name'],specification['version'],digest,to_jsonb(specification)))
        cur.execute('''SELECT id,fingerprint FROM public.ai_regression_suites
            WHERE workspace_id=%s::uuid AND name=%s AND version=%s''',
            (workspace,specification['name'],specification['version']))
        row = dict(cur.fetchone())
        if row['fingerprint'] != digest: raise ValueError('suite_version_is_immutable')
        return row


def get_suite(workspace, suite_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM public.ai_regression_suites WHERE workspace_id=%s::uuid AND id=%s::uuid',
                    (workspace,suite_id))
        row=cur.fetchone()
        if not row: raise ValueError('regression_suite_not_found')
        return dict(row)


def claim_turn(workspace, suite_id, scenario_key, run_id, step_index, versions):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO public.ai_regression_runs(id,workspace_id,suite_id,scenario_key,versions)
            VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s) ON CONFLICT(id) DO NOTHING''',
            (run_id,workspace,suite_id,scenario_key,to_jsonb(versions)))
        cur.execute('SELECT * FROM public.ai_regression_runs WHERE id=%s::uuid AND workspace_id=%s::uuid FOR UPDATE',
                    (run_id,workspace))
        row=cur.fetchone()
        if not row or str(row['suite_id'])!=str(suite_id) or row['scenario_key']!=scenario_key:
            raise ValueError('regression_run_scope_mismatch')
        row=dict(row)
        if step_index < row['next_step']: return row,False
        if row['versions'] != versions: raise ValueError('regression_configuration_changed')
        if step_index!=row['next_step'] or row['active_step'] is not None:
            raise ValueError('regression_turn_out_of_order_or_running')
        cur.execute('''UPDATE public.ai_regression_runs SET active_step=%s,active_since=now()
            WHERE id=%s::uuid AND workspace_id=%s::uuid''',(step_index,run_id,workspace))
        return row,True


def finish_turn(workspace,run_id,step_index,result,state,simulation_state,completed):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE public.ai_regression_runs
            SET turns=turns || %s::jsonb, state=%s, simulation_state=%s,
                next_step=next_step+1,active_step=NULL,active_since=NULL,
                status=%s,finished_at=CASE WHEN %s THEN now() ELSE NULL END
            WHERE id=%s::uuid AND workspace_id=%s::uuid AND active_step=%s RETURNING *''',
            (to_jsonb([result]),to_jsonb(state),to_jsonb(simulation_state),
             'completed' if completed else 'running',completed,run_id,workspace,step_index))
        row=cur.fetchone()
        if not row: raise ValueError('regression_turn_claim_lost')
        return dict(row)


def get_run(workspace,run_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM public.ai_regression_runs WHERE id=%s::uuid AND workspace_id=%s::uuid',(run_id,workspace))
        row=cur.fetchone()
        if not row: raise ValueError('regression_run_not_found')
        return dict(row)
