"""Fixed-denominator scoring: missing, failed and inconclusive cases score zero."""
from collections import Counter


def summarize(suite, reports, *, split='all', repetitions=None):
    cases=[s for s in suite.scenarios if split=='all' or s.split==split]
    rows=[]; critical=[]; versions=set(); latencies=[]; calls=0
    for case in cases:
        report=reports.get(case.key)
        turns=(report or {}).get('turns') or []
        complete=bool(report and report.get('status')=='completed' and len(turns)==len(case.steps))
        passed=complete and all(t.get('grade',{}).get('outcome')=='passed' for t in turns)
        rows.append({'key':case.key,'category':case.category,'split':case.split,
                     'passed':passed,'complete':complete,'critical':case.critical})
        for turn in turns:
            critical.extend({'case':case.key,'step':turn['step'],'error':e}
                            for e in turn.get('grade',{}).get('critical_errors',[]))
            replay=turn.get('replay') or {}
            if replay.get('elapsed_ms') is not None: latencies.append(replay['elapsed_ms'])
            calls+=replay.get('real_model_calls',0)
        if report:
            v=report.get('versions') or {}
            versions.add(tuple(str(v.get(k)) for k in ('configuration_hash','persona','model','judge_model','deployment')))
    categories={}
    for category in suite.categories:
        group=[r for r in rows if r['category']==category]
        categories[category]={'total':len(group),'passed':sum(r['passed'] for r in group),
            'percent':100*sum(r['passed'] for r in group)/len(group) if group else 0}
    score=sum(c['percent'] for c in categories.values())/len(categories)
    validation=[r for r in rows if r['split']=='validation']
    validation_percent=100*sum(r['passed'] for r in validation)/len(validation) if validation else 0
    repeated_total = repeated_passed = 0
    repeated_complete = True
    for case in [s for s in cases if s.critical]:
        # First run + two independent repeats. Missing runs remain failures.
        repeated = list((repetitions or {}).get(case.key, []))
        samples = ([reports[case.key]] if case.key in reports else []) + repeated
        repeated_complete &= len(samples) >= 3
        repeated_total += max(3, len(samples))
        for sample in samples:
            turns = sample.get('turns') or []
            repeated_passed += bool(sample.get('status') == 'completed'
                and len(turns) == len(case.steps)
                and all(t.get('grade', {}).get('outcome') == 'passed' for t in turns))
            v = sample.get('versions') or {}
            versions.add(tuple(str(v.get(k)) for k in ('configuration_hash','persona','model','judge_model','deployment')))
            if sample in repeated:
                critical.extend({'case':case.key,'step':turn['step'],'error':error}
                    for turn in turns for error in turn.get('grade',{}).get('critical_errors',[]))
    repeat_percent = 100 * repeated_passed / repeated_total if repeated_total else 0
    gates={'score_above_target':score>suite.target_percent,
        'category_floor':all(c['percent']>=suite.minimum_category_percent for c in categories.values()),
        'coverage':all(c['total']>=suite.minimum_cases_per_category for c in categories.values()),
        'no_critical_errors':not critical,
        'single_candidate':len(versions)==1,
        'validation':len(validation)>=20 and validation_percent>suite.target_percent,
        'critical_repetitions':bool(repeated_total) and repeated_complete and repeat_percent>=95,
        'all_executed':all(r['complete'] for r in rows)}
    latency=sorted(latencies)
    return {'score_percent':round(score,2),'categories':categories,'gates':gates,
        'campaign_passed':all(gates.values()),'total':len(rows),'passed':sum(r['passed'] for r in rows),
        'unexecuted_or_incomplete':sum(not r['complete'] for r in rows),
        'validation_total':len(validation),'validation_percent':round(validation_percent,2),
        'critical_repetition_total':repeated_total, 'critical_repetition_passed':repeated_passed,
        'critical_repetition_percent':round(repeat_percent,2),
        'critical_errors':critical,'failed_cases':[r['key'] for r in rows if not r['passed']],
        'real_model_calls':calls,'latency_ms':{'median':latency[len(latency)//2] if latency else None,
            'p95':latency[min(len(latency)-1,int(len(latency)*.95))] if latency else None}}
