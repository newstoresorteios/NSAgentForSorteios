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
    args=parser.parse_args()
    replay=json.loads(args.report.read_text(encoding='utf-8'))
    expected=Expectations.model_validate_json(args.expected.read_text(encoding='utf-8'))
    report=audit_replay(replay,expected)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return int(bool(report['findings']))


if __name__=='__main__':
    raise SystemExit(main())
