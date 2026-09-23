"""Conservative, durable reservations shared by every worker in a campaign.

Reservations are never refunded on timeouts: provider usage may be unknown.
Rates are operator-supplied, versioned USD per million tokens, not model guesses.
"""
from __future__ import annotations
import hashlib
import json
from decimal import Decimal

from app.configuration.runtime import current_bundle
from app.ops.turn_runtime import LLMCallBudgetExceeded


class EvaluationBudgetExceeded(LLMCallBudgetExceeded):
    pass


def reservation(policy, *, model, messages, output_limit):
    if policy.get('enabled') is not True:
        raise EvaluationBudgetExceeded('evaluation_campaign_disabled')
    if not policy.get('campaign_id') or not policy.get('price_version'):
        raise EvaluationBudgetExceeded('evaluation_campaign_identity_or_prices_missing')
    # UTF-8 byte count is a conservative text-token estimate. Include room for
    # schema/protocol overhead; image/audio campaigns need an explicit estimator.
    payload = json.dumps(messages or [], ensure_ascii=False,
                         default=lambda value: value.model_json_schema() if hasattr(value, 'model_json_schema') else str(value))
    if any(key in payload for key in ('image_url', 'input_image', 'input_audio')):
        raise EvaluationBudgetExceeded('evaluation_media_budget_not_supported')
    max_input = int(policy.get('max_input_tokens_per_call') or 0)
    estimate = len(payload.encode('utf-8')) + 16384
    if not output_limit or max_input < estimate:
        raise EvaluationBudgetExceeded('evaluation_call_bound_missing_or_exceeded')
    rates = policy.get('prices', {}).get(model, {})
    if not all(k in rates for k in ('input', 'output')):
        raise EvaluationBudgetExceeded('evaluation_model_price_missing')
    input_rate, output_rate = Decimal(str(rates['input'])), Decimal(str(rates['output']))
    if not input_rate.is_finite() or not output_rate.is_finite() or min(input_rate, output_rate) < 0:
        raise EvaluationBudgetExceeded('evaluation_model_price_invalid')
    tokens = max_input + int(output_limit)
    cost = (max_input * input_rate + int(output_limit) * output_rate) / 1000000
    if (int(policy.get('max_calls') or 0) < 1 or tokens > int(policy.get('max_tokens') or 0)
            or cost > Decimal(str(policy.get('max_cost_usd') or 0))):
        raise EvaluationBudgetExceeded('evaluation_campaign_budget_exceeded')
    return tokens, cost


def reserve_evaluation_call(*, model, messages, output_limit):
    bundle = current_bundle()
    raw = bundle.get('values', {}).get('evaluationCampaignPolicy', '{}')
    policy = json.loads(raw) if isinstance(raw, str) else raw
    tokens, cost = reservation(policy, model=model, messages=messages, output_limit=output_limit)
    workspace = bundle.get('workspaceId')
    if not workspace:
        raise EvaluationBudgetExceeded('evaluation_workspace_missing')
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    # Domain replay connections remain READ ONLY. This separate accounting
    # transaction can touch only the fixed budget table, never commerce data.
    import psycopg
    from app.config import get_settings
    with psycopg.connect(get_settings().database_url, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO public.ai_evaluation_campaign_budgets
            (workspace_id,campaign_key,policy_hash) VALUES (%s::uuid,%s,%s)
            ON CONFLICT DO NOTHING''', (workspace,policy['campaign_id'],digest))
        cur.execute('''UPDATE public.ai_evaluation_campaign_budgets
            SET reserved_calls=reserved_calls+1,reserved_tokens=reserved_tokens+%s,
                reserved_cost_usd=reserved_cost_usd+%s,updated_at=now()
            WHERE workspace_id=%s::uuid AND campaign_key=%s AND policy_hash=%s
              AND reserved_calls+1<=%s AND reserved_tokens+%s<=%s
              AND reserved_cost_usd+%s<=%s RETURNING reserved_calls''',
            (tokens,cost,workspace,policy['campaign_id'],digest,policy['max_calls'],tokens,
             policy['max_tokens'],cost,policy['max_cost_usd']))
        if cur.fetchone() is None:
            raise EvaluationBudgetExceeded('evaluation_campaign_budget_exceeded_or_changed')
