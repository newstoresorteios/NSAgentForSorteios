"""Check reviewed evidence against the entire required corpus; no deployment side effects."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evaluation.release_evidence import assess_release_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true', help='Validate offline evidence only; never approves production')
    args = parser.parse_args()
    load = lambda path: json.loads(path.read_text(encoding='utf-8'))
    corpus = load(args.corpus)
    required = [step['case_id'] for convo in corpus['conversations'] for step in convo['steps']]
    result = assess_release_evidence(load(args.manifest), load(args.results), required,
                                     production_required=not args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['approved'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
