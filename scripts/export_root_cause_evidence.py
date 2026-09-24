"""Export synthetic test outcomes without credentials, customer data or raw traces."""
import argparse
import json
from pathlib import Path


def export(final_reserved):
    folders = ['consistency-root-cause-candidate', 'consistency-root-cause-final',
               'consistency-root-cause-delivery']
    turns = []
    for folder in folders:
        for path in (Path('evals/results') / folder).glob('*.json'):
            report = json.loads(path.read_text(encoding='utf-8'))
            for turn in report.get('turns', []):
                replay = turn.get('replay') or {}
                metadata = replay.get('metadata') or {}
                turns.append({
                    'iteration': folder, 'scenario': report['scenario_key'],
                    'run_id': report['id'], 'step': turn['step'], 'input': turn['input'],
                    'reply': replay.get('reply'), 'automated_outcome': turn['grade'].get('outcome'),
                    'automated_summary': turn['grade'].get('summary'),
                    'objective_failures': turn['grade'].get('objective_failures'),
                    'safety_reason': replay.get('safety_reason'),
                    'budget_errors': replay.get('budget_errors'),
                    'preferences': replay.get('next_state', {}).get('active_preferences'),
                    'final_validation': metadata.get('final_response_validation'),
                    'technical_completeness': metadata.get('inspection_requested_facts_covered'),
                })
    path = Path('docs/audits/2026-09-24/root-cause-live-evidence.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'base_commit': 'a88e8d4', 'production_promoted': False,
        'authorized_total_usd': 10, 'prior_reserved_usd': 4.26891675,
        'candidate_reserved_usd': 4.20220425, 'final_tranche_reserved_usd': final_reserved,
        'total_reserved_usd': round(4.26891675 + 4.20220425 + final_reserved, 8),
        'note': 'Reservation is conservative, not the provider invoice. All campaigns stopped.',
        'environment': 'Isolated candidates, production OpenAI/configuration, read-only TRAYadaptor',
        'limitations': ['No real channel delivery, media, checkout or orders',
            'Seeded final replays are targeted regressions, not another full journey',
            'Automated judge penalizes omitted repetition of preserved preferences; manual review required'],
        'manual_review': {
            'screenshot_orient_2500': 'No generic outage. Brand Orient and budget 2500 retained; clarification continues. Final seeded test also includes historical shipped order. Objective state assertions passed.',
            'qualification_changes': 'Orient and 3500 retained through quartz replacement and movement/strap release. Released search delivered an Orient Classic. Final local tests additionally retain the required strap marker through replacement.',
            'technical_and_reset': 'Restart clears reference/EAN; no recipient-name poisoning in replay. Final post-reset search preserved Orient/social/2500 without handoff.',
            'delivery:technical_and_reset_replay_2': 'Passed real replay and automated review: NY0120-01EE, Pix 2889.99, automatic/mineral/200m/black/rubber/8204, explicit 41-vs-42mm discrepancy. Final validators passed.',
            'delivery:orient_identity_negative_replay_1': 'Real agent reply complete and final deterministic validators passed: orange/sapphire/automatic/45mm/Pix3144.99, above budget, catalog availability30businessdays, no tomorrow delivery promise. External automated judge blocked by final campaign budget/call limit; retain inconclusive automated outcome, do not claim a judge pass.',
        },
        'turns': turns,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(path), 'turns': len(turns)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final-reserved', type=float, required=True)
    export(parser.parse_args().final_reserved)
