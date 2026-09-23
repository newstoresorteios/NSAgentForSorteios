"""Audit a local minimized replay without API calls or production writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation.regression_models import Expectations
from app.evaluation.conversation_auditor import audit_replay


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--expected',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--semantic',action='store_true',help='Use the published paid campaign budget')
    parser.add_argument('--workspace',help='Required for semantic audit')
    args=parser.parse_args()
    replay=json.loads(args.report.read_text(encoding='utf-8'))
    expected=Expectations.model_validate_json(args.expected.read_text(encoding='utf-8'))
    if args.semantic:
        if not args.workspace:parser.error('--semantic requires --workspace')
        import asyncio
        from app.persona.persona_runtime import load_persona_runtime
        from app.configuration.runtime import bind_bundle,reset_bundle,settings_from_bundle
        from app.config import get_settings
        from app.evaluation.semantic_auditor import audit_conversation
        persona=load_persona_runtime(workspace_id=args.workspace)
        bundle=persona.configuration_bundle
        token=bind_bundle(bundle,settings_from_bundle(get_settings(),bundle))
        try:report=asyncio.run(audit_conversation(replay,expected))
        finally:reset_bundle(token)
    else:
        report=audit_replay(replay,expected)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return int(bool(report['findings']) or (args.semantic and report.get('outcome')!='passed'))


if __name__=='__main__':
    raise SystemExit(main())
