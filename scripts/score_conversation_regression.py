import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evaluation.regression_models import RegressionSuite
from app.evaluation.regression_score import summarize

p=argparse.ArgumentParser()
p.add_argument('--suite',type=Path,required=True)
p.add_argument('--runs',type=Path,required=True,action='append')
p.add_argument('--repeat-runs',type=Path,action='append',default=[])
p.add_argument('--split',choices=['all','development','validation'],default='all')
args=p.parse_args()
suite=RegressionSuite.model_validate_json(args.suite.read_text(encoding='utf-8'))
reports={}
repetitions={}
seen=set()
for directory in args.runs+args.repeat_runs:
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['runs']:
        if item['run_id'] in seen: continue
        seen.add(item['run_id'])
        path=directory/(item['run_id']+'.json')
        if not path.exists(): continue
        report=json.loads(path.read_text(encoding='utf-8'))
        if directory in args.repeat_runs or item['repetition']!=0:
            repetitions.setdefault(item['scenario'],[]).append(report)
        elif item['scenario'] in reports:
            p.error('duplicate primary scenario: '+item['scenario'])
        else: reports[item['scenario']]=report
result=summarize(suite,reports,split=args.split,repetitions=repetitions)
(args.runs[0]/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
