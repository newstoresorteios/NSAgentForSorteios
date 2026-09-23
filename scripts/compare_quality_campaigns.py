"""Compare two local campaign summaries without new model calls."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evaluation.comparison import compare_samples


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    a=json.loads(args.baseline.read_text(encoding='utf-8'));b=json.loads(args.candidate.read_text(encoding='utf-8'))
    limits=b.get('comparison_limits') or {}
    result=compare_samples(a.get('samples',[]),b.get('samples',[]),
        max_cost_ratio=limits.get('max_cost_ratio',1),max_latency_ratio=limits.get('max_latency_ratio',1))
    result['candidate_coverage_passed']=b.get('campaign_passed') is True
    # Operator review remains mandatory; this command never flips a rollout flag.
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    return 0 if all(result[k] for k in ('quality_non_regression','candidate_coverage_passed','cost_within_limit','latency_within_limit')) else 1


if __name__=='__main__':raise SystemExit(main())
