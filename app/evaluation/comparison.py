"""Compare completed, like-for-like evaluation samples; missing data never passes."""
from statistics import median
import math


def summarize_samples(rows):
    completed = [r for r in rows if r.get('complete')]
    latencies = sorted(r['latency_ms'] for r in completed if r.get('latency_ms') is not None)
    return {'total':len(rows), 'complete':len(completed),
        'passed':sum(r.get('passed') is True for r in completed),
        'critical_errors':sum(len(r.get('critical_errors', [])) for r in rows),
        'cost_usd':sum(r['cost_usd'] for r in rows) if rows and all(r.get('cost_usd') is not None for r in rows) else None,
        'tokens':sum(r['tokens'] for r in rows) if rows and all(r.get('tokens') is not None for r in rows) else None,
        'calls':sum(r['calls'] for r in rows) if rows and all(r.get('calls') is not None for r in rows) else None,
        'median_ms':median(latencies) if latencies else None,
        'p95_ms':latencies[max(0,math.ceil(len(latencies)*.95)-1)] if latencies else None}


def compare_samples(baseline, candidate, *, max_cost_ratio=1.0, max_latency_ratio=1.0):
    if any(not math.isfinite(v) or v <= 0 for v in (max_cost_ratio,max_latency_ratio)):
        raise ValueError('comparison_limits_invalid')
    keys = ('case_id','repetition','split','fixture_hash','persona_hash','code_hash')
    identity = lambda r: tuple(r.get(k) for k in keys)
    before_ids, after_ids = [identity(r) for r in baseline], [identity(r) for r in candidate]
    comparable = (bool(before_ids) and len(before_ids)==len(set(before_ids))
                  and len(after_ids)==len(set(after_ids)) and set(before_ids)==set(after_ids)
                  and all(all(v is not None for v in key) for key in before_ids))
    before, after = summarize_samples(baseline), summarize_samples(candidate)
    cost_ok = (comparable and before['cost_usd'] is not None and after['cost_usd'] is not None
               and after['cost_usd'] <= before['cost_usd'] * max_cost_ratio)
    latency_ok = (comparable and before['p95_ms'] is not None and after['p95_ms'] is not None
                  and after['p95_ms'] <= before['p95_ms'] * max_latency_ratio
                  and all(r.get('latency_ms') is not None for r in [*baseline,*candidate]))
    return {'comparable':comparable,'baseline':before,'candidate':after,
        'cost_within_limit':cost_ok,'latency_within_limit':latency_ok,
        'quality_non_regression': comparable and after['complete']==after['total']
            and before['complete']==before['total'] and after['passed']>=before['passed']
            and after['critical_errors']==0,
        # This comparison is not a release score. The full existing coverage,
        # held-out validation and critical-repeat gates still apply.
        'release_approved':False}
