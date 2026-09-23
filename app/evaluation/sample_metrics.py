"""Conservative comparable measurements; unknown usage stays unknown."""


def campaign_samples(suite,reports,repeats,prices):
    samples=[]
    for case in suite.scenarios:
        for repetition,report in enumerate([reports.get(case.key),*repeats.get(case.key,[])],1):
            if not report:continue
            calls=[];usage_complete=True
            for turn in report.get('turns',[]):
                runtime=turn.get('replay',{}).get('runtime',{})
                recorded=runtime.get('openai_calls',[])
                usage_complete &= len(recorded)==runtime.get('openai_transport_attempts',len(recorded))
                calls.extend(recorded)
                grade=turn.get('grade',{})
                if grade.get('model_usage'):
                    calls.append({**grade['model_usage'],'ok':True})
                elif not grade.get('judge_skipped') and not grade.get('execution_errors'):
                    usage_complete=False
            cost=0;tokens=0
            for call in calls:
                rates=prices.get(call.get('model'))
                if not rates or call.get('ok') is not True:
                    usage_complete=False;continue
                i,o=call.get('input_tokens'),call.get('output_tokens')
                if i is None or o is None:
                    usage_complete=False;continue
                cached=min(i,call.get('cached_tokens',0))
                cost+=((i-cached)*rates['input']+cached*rates.get('cached_input',rates['input'])+o*rates['output'])/1e6
                tokens+=i+o
            v=report.get('versions',{})
            complete=report.get('status')=='completed'
            samples.append({'case_id':case.key,'split':case.split,'repetition':repetition,
                'fixture_hash':v.get('suite'),'persona_hash':v.get('persona'),'code_hash':v.get('deployment'),
                'complete':complete,'passed':complete and all(t['grade']['outcome']=='passed' for t in report.get('turns',[])),
                'critical_errors':[e for t in report.get('turns',[]) for e in t['grade'].get('critical_errors',[])],
                'latency_ms':report.get('elapsed_ms'),'calls':len(calls) if usage_complete else None,
                'tokens':tokens if usage_complete else None,'cost_usd':cost if usage_complete else None})
    return samples
