from __future__ import annotations

import json
from app.configuration.runtime import message, policy
from app.evaluation.regression_models import RegressionVerdict
from app.llm.openai_gateway import parse_structured_output


def at_path(value, path):
    for part in path.split('.'):
        if not isinstance(value, dict): return None
        value = value.get(part)
    return value


def objective_checks(replay, expected):
    failures = []
    tools = {c['tool'] for c in replay.get('tools', [])}
    for name in expected.required_tools:
        if name not in tools: failures.append('required_tool_missing:' + name)
    for name in expected.forbidden_tools:
        if name in tools: failures.append('forbidden_tool_called:' + name)
    if expected.handoff == 'required' and not replay.get('handoff'): failures.append('handoff_missing')
    if expected.handoff == 'forbidden' and replay.get('handoff'): failures.append('unexpected_handoff')
    offer = ((replay.get('metadata') or {}).get('handoff') or {}).get('offer') is True
    if expected.handoff_offer == 'required' and not offer: failures.append('handoff_offer_missing')
    if expected.handoff_offer == 'forbidden' and offer: failures.append('unexpected_handoff_offer')
    for path, value in expected.state_equals.items():
        if at_path(replay.get('next_state') or {}, path) != value:
            failures.append('state_mismatch:' + path)
    products = (replay.get('commercial_data') or {}).get('products') or []
    if len(products) < expected.min_products: failures.append('insufficient_products')
    for product in products:
        pid = str(product.get('id',product.get('product_id','')))
        if expected.allowed_product_ids and pid not in expected.allowed_product_ids:
            failures.append('unexpected_product:' + pid)
        if expected.max_price is not None:
            from app.catalog.retrieval.price import resolve_commercial_price
            price = resolve_commercial_price(product, require_positive=True).amount
            if price is None or price > expected.max_price: failures.append('product_budget_mismatch:' + pid)
    if not replay.get('reply','').strip(): failures.append('empty_reply')
    factual = (replay.get('metadata') or {}).get('factual_validation') or {}
    if factual.get('valid') is False and not factual.get('fallback_applied'):
        failures.append('factual_validation_failed')
    final = (replay.get('metadata') or {}).get('final_response_validation') or {}
    if final.get('remaining_issues'): failures.append('remaining_final_validation_issues')
    return failures


def trim_evidence(value):
    if isinstance(value,list): return [trim_evidence(v) for v in value]
    if isinstance(value,dict):
        return {k:trim_evidence(v) for k,v in value.items()
                if k not in {'review_inputs','evidence_preview','raw','html'}}
    if isinstance(value,str) and len(value)>8000: return value[:8000]
    return value


def compact_catalog_evidence(facts, tools):
    """Deduplicate repeated sheets without losing conflicting versions or media."""
    evidence = {}
    fingerprints = {}
    def visit(value):
        if isinstance(value, list):
            return [visit(v) for v in value]
        if not isinstance(value, dict):
            return value
        # These describe retrieval internals, not the merchant's product facts.
        cleaned = {k: v for k, v in value.items() if k not in {
            '_retrieval', '_freshness_at', '_field_sources', '_factual_source',
            '_catalog_item_key', '_revalidated', 'elapsed_ms',
        }}
        if cleaned.get('id') and (cleaned.get('name') or cleaned.get('reference')):
            fingerprint = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str)
            if fingerprint not in fingerprints:
                key = 'sheet_' + str(len(evidence) + 1)
                fingerprints[fingerprint] = key
                evidence[key] = cleaned
            return {'evidence_ref': fingerprints[fingerprint]}
        return {k: visit(v) for k, v in cleaned.items()}
    return {'facts': visit(trim_evidence(facts)), 'tools': visit(trim_evidence(tools)),
            'catalog_evidence': evidence}


async def grade_turn(scenario, step, replay, history, persona):
    expected = step.expected
    failures = objective_checks(replay, expected)
    if (scenario is not None and scenario.simulation.get('faults')
            and not any((c.get('result') or {}).get('_simulated_fault') for c in replay.get('tools',[]))):
        failures.append('injected_fault_not_exercised')
    critical = [f for f in failures if f.startswith('forbidden_tool_called:')]
    unexpected = [c['tool'] for c in replay.get('tools',[]) if (c.get('result') or {}).get('error')
                  and not (c['tool'] in expected.expected_tool_errors and (c['result'].get('_simulated_fault')
                          or c['result'].get('status_code')==404))]
    if replay.get('error') or replay.get('blocked') or unexpected:
        return {'outcome':'failed' if critical else 'inconclusive','objective_failures':failures,
                'execution_errors':[replay.get('error'),*replay.get('blocked',[]),*unexpected],
                'critical_errors':critical}
    criteria = expected.requirements + ['NÃO: ' + value for value in expected.forbidden_claims]
    from app.verify.persona_evidence import persona_evidence
    metadata = replay.get('metadata') or {}
    payload = {'criteria':criteria, 'history':history, 'customer':step.input,
               'reply':replay['reply'],
               **compact_catalog_evidence(replay.get('commercial_data') or {}, replay.get('tools') or []),
               'state':replay.get('next_state'),
               'metadata':{k:metadata[k] for k in ('interpretation', 'response_source',
                   'final_response_validation', 'product_resolution_state', 'outbound_image_url',
                   'outbound_images', 'conversation_repair') if k in metadata},
               'objective_failures':failures,
               'environment':scenario.environment}
    payload['published_persona']=persona_evidence((replay.get('commercial_data') or {}).get('products'),persona)
    result = await parse_structured_output(model=str(policy('historyEvaluationModel')),
        text_format=RegressionVerdict, messages=[{'role':'system','content':message('regression_judge')+'\n'+message('judge_commercial_policy')},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False,default=str)}],
        temperature=0, call_type='regression_evaluation',
        timeout_seconds=float(policy('historyEvaluationJudgeTimeout')))
    verdict = result.parsed.model_dump(mode='json')
    if sorted(c['criterion'] for c in verdict['criteria']) != sorted(criteria):
        failures.append('judge_omitted_or_changed_criteria')
    critical = list(verdict['critical_errors'])
    critical.extend(f for f in failures if f.startswith('forbidden_tool_called:'))
    passed = not (failures or verdict['factual_errors'] or critical) and all(c['passed'] for c in verdict['criteria'])
    return {**verdict, 'outcome':'passed' if passed else 'failed',
            'objective_failures':failures,'critical_errors':critical}
