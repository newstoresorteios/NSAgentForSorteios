"""Export synthetic transcript and verdicts, excluding credentials and raw state."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = []
    for path in sorted(args.input.glob('*.json')):
        artifact = json.loads(path.read_text(encoding='utf-8'))
        report = artifact.get('report') or {}
        for turn in report.get('turns', []):
            replay = turn.get('replay') or {}
            metadata = replay.get('metadata') or {}
            records.append({
                'artifact': path.name,
                'previous_artifact': artifact.get('previous_artifact'),
                'versions': report.get('versions'),
                'customer': turn['input'], 'agent': replay.get('reply'),
                'grade': turn.get('grade'),
                'question_slot': (metadata.get('discovery_question') or {}).get('slot'),
                'elapsed_ms': replay.get('elapsed_ms'),
                'real_model_calls': replay.get('real_model_calls'),
                'budget_errors': replay.get('budget_errors'),
                'tool_names': [t['tool'] for t in replay.get('tools', [])],
            })
    result = {'scope': 'Real OpenAI and live read-only catalog on isolated candidate; no customer messages or orders.',
              'records': records,
              'note': 'Failures and corrected repetitions are retained separately. Planned scenarios are not executed evidence.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'turns': len(records)}))


if __name__ == '__main__':
    main()
