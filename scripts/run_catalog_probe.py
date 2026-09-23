"""One operator-selected follow-up, preserving real isolated conversation history."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_launch_conversation_suite import build, MUTATIONS
from scripts.run_conversation_regression import Client
from app.evaluation.regression_runner import replay_history
from app.evaluation.regression_models import RegressionSuite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--requirement', action='append', required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--allow-deployment-change', action='store_true',
                        help='Explicitly replay a prior conversation against a corrected candidate')
    parser.add_argument('--allow-ungraded-followup', action='store_true',
                        help='Continue an actual reply when only its judge was blocked; budget still enforced')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--access-file', type=Path, required=True)
    parser.add_argument('--vercel-cli', type=Path, required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; do not repeat a possibly charged turn')
    access = json.loads(args.access_file.read_text(encoding='utf-8'))
    client = Client(SimpleNamespace(access_file=args.access_file, base_url=access['base_url'],
                                   vercel_cli=args.vercel_cli, vercel_auth_dir=None))
    history, state = [], {}
    if args.previous:
        previous = json.loads(args.previous.read_text(encoding='utf-8'))
        if previous['base_url'] != access['base_url'] and not args.allow_deployment_change:
            parser.error('follow-up must use the same deployment')
        report = previous['report']
        turns = report['turns']
        if not turns or not turns[-1].get('replay', {}).get('reply'):
            parser.error('previous turn has no actual reply')
        if (turns[-1].get('grade', {}).get('evaluation_block')
                and (not args.allow_ungraded_followup or turns[-1]['replay'].get('budget_errors'))):
            parser.error('previous turn exhausted the campaign budget')
        history = replay_history(previous['scenario']['history'], turns)
        state = turns[-1]['replay'].get('next_state', {})
    suite = build()
    run_id = str(uuid4())
    suite.update(name='catalog-adaptive-' + run_id, version=1, minimum_cases_per_category=1)
    budget = json.loads(suite['configuration_overrides']['evaluationCampaignPolicy'])
    # Earlier campaigns are stopped with USD 21.78337575 reserved. User approved
    # USD 25 total; this USD 3.20 tranche stays below it. Do not resume old tranches.
    budget.update(campaign_id='launch-20260923-orient25', max_cost_usd=3.20, max_calls=36)
    suite['configuration_overrides']['evaluationCampaignPolicy'] = json.dumps(budget)
    scenario = {'key': 'catalog_probe', 'category': 'conversa', 'split': 'development',
                'critical': True, 'environment': 'live_readonly', 'history': history,
                'initial_state': state, 'steps': [{'input': args.input, 'expected': {
                    'requirements': args.requirement, 'forbidden_tools': MUTATIONS}}]}
    suite['scenarios'] = [scenario]
    suite = RegressionSuite.model_validate(suite).model_dump(mode='json')
    registered = client.request('/api/admin/regression/suites',
                                {'workspace_id': args.workspace, 'specification': suite})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifact = {'base_url': access['base_url'], 'run_id': run_id, 'suite_id': registered['id'],
                'scenario': scenario, 'report': None,
                'previous_artifact': str(args.previous) if args.previous else None,
                'previous_deployment': previous['base_url'] if args.previous else None}
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
    # Checkpoint precedes the request: on transport error fetch run_id, never re-run blindly.
    report = client.request('/api/admin/regression/turn', {
        'workspace_id': args.workspace, 'suite_id': registered['id'],
        'scenario_key': 'catalog_probe', 'run_id': run_id, 'step_index': 0})
    artifact['report'] = report
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
    turn = report['turns'][-1]
    print(json.dumps({'input': args.input, 'reply': turn.get('replay', {}).get('reply'),
                      'grade': turn.get('grade'), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
