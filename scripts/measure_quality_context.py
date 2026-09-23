"""Offline token measurement on frozen suite state and facts, not full API prompts."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure(suite, encoding):
    from app.llm.compact_context import compact_payload
    from app.commerce.commerce_context import CommerceConversationState
    from app.memory.working_memory import build_working_memory
    rows=[]
    for case in suite['scenarios']:
        state=CommerceConversationState.model_validate(case['initial_state'])
        payload={'STATE_FACTS':state.interpreter_payload(),
                 'WORKING_MEMORY':build_working_memory(state),
                 'FACTS':case['simulation'].get('products',[])}
        compact=compact_payload(payload)
        if compact is not payload:
            from app.configuration.runtime import message
            compact['context_reference_instructions']=message('context_reference_instructions')
        before=json.dumps(payload,ensure_ascii=False,default=str)
        after=json.dumps(compact,ensure_ascii=False,default=str,separators=(',',':'))
        if len(after)>=len(before):after=before
        a,b=len(encoding.encode(before)),len(encoding.encode(after))
        rows.append({'case':case['key'],'before_tokens':a,'after_tokens':b})
    before=median(r['before_tokens'] for r in rows);after=median(r['after_tokens'] for r in rows)
    return {'scope':'frozen_state_and_catalog_context_only','encoding':encoding.name,
            'before_median_tokens':before,'after_median_tokens':after,
            'median_reduction_percent':round(100*(before-after)/before,2),
            'paid_calls':0,'rows':rows,'release_approved':False}


def main():
    import tiktoken
    from app.configuration.runtime import bind_bundle, reset_bundle
    p=argparse.ArgumentParser();p.add_argument('--suite',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    definitions=json.loads(Path('sql/seeds/operator_catalog.json').read_text(encoding='utf-8'))
    token=bind_bundle({'fields':definitions,'values':{f['key']:f['default'] for f in definitions}},None)
    try:report=measure(json.loads(args.suite.read_text(encoding='utf-8')),tiktoken.get_encoding('o200k_base'))
    finally:reset_bundle(token)
    report['suite_hash']=hashlib.sha256(args.suite.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}))


if __name__=='__main__':main()
