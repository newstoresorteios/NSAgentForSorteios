"""Manual runner. Uses published campaign budget; never enables it automatically."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    from app.evaluation.regression_models import RegressionSuite
    from app.evaluation.controlled_campaign import run_campaign
    from app.persona.persona_runtime import load_persona_runtime
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',required=True);p.add_argument('--suite',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--overrides',type=Path)
    args=p.parse_args()
    suite=RegressionSuite.model_validate_json(args.suite.read_text(encoding='utf-8'))
    persona=load_persona_runtime(workspace_id=args.workspace)
    digest=hashlib.sha256()
    for file in sorted(Path('app').rglob('*.py')):
        digest.update(str(file).encode());digest.update(file.read_bytes())
    result=asyncio.run(run_campaign(suite,persona,output_dir=args.output,code_hash=digest.hexdigest(),
        overrides=json.loads(args.overrides.read_text(encoding='utf-8')) if args.overrides else None))
    print(json.dumps({'campaign_passed':result['campaign_passed'],'gates':result['gates'],
                      'total':result['total'],'passed':result['passed']}))
    return 0 if result['campaign_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
