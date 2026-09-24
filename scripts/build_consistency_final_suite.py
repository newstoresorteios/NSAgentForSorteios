"""Replay the exact failing states with a final $1.50 reservation tranche.

Previous tranches are stopped at $4.26891675 + $4.20220425. The maximum
combined reservation is therefore $9.971121, below the authorized $10.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_consistency_suite import build_suite
from app.evaluation.regression_models import RegressionSuite


def build():
    suite = build_suite(candidate=True)
    suite.update(name='consistency-root-cause-final', version=1)
    budget = json.loads(suite['configuration_overrides']['evaluationCampaignPolicy'])
    budget.update(campaign_id='consistency-root-cause-final150-20260924', max_cost_usd=1.5, max_calls=20)
    suite['configuration_overrides']['evaluationCampaignPolicy'] = json.dumps(budget)
    folder = Path('evals/results/consistency-root-cause-candidate')
    reports = {r['scenario_key']: r for path in folder.glob('*.json')
               if path.name != 'manifest.json' for r in [json.loads(path.read_text(encoding='utf-8'))]}
    selected = []
    for key, index in [('technical_and_reset', 2), ('orient_identity_negative', 1),
                       ('screenshot_orient_2500', 3), ('technical_and_reset', 4)]:
        original = next(s for s in suite['scenarios'] if s['key'] == key)
        prior = reports[key]['turns'][:index]
        scenario = dict(original)
        scenario['key'] = key + '_replay_' + str(index)
        scenario['steps'] = [original['steps'][index]]
        scenario['initial_state'] = prior[-1]['replay']['next_state']
        scenario['history'] = []
        for turn in prior:
            scenario['history'].extend([
                {'role': 'user', 'content': turn['input']},
                {'role': 'assistant', 'content': turn['replay']['reply'],
                 'metadata': {k: v for k, v in turn['replay']['metadata'].items()
                              if k in {'interpretation', 'active_preferences', 'discovery_question',
                                       'adaptive_discovery', 'safety_reason', 'dialogue_phase', 'domain'}}}])
        if key == 'orient_identity_negative':
            scenario['steps'][0]['input'] = 'Confirme cor, vidro, movimento, tamanho, Pix e prazo. Então é azul, mineral, até R$ 3.000 no Pix e chega amanhã?'
        if key == 'screenshot_orient_2500':
            scenario['initial_state'].update(order_id='evaluation-historical-order',
                order_status_group='shipped', order_payment_status='unknown')
            scenario['steps'][0]['expected']['state_equals'] = {
                'active_preferences.subject_brand': 'Orient',
                'active_preferences.budget_max': 2500,
            }
        selected.append(scenario)
    suite['scenarios'] = selected
    return RegressionSuite.model_validate(suite).model_dump(mode='json')


if __name__ == '__main__':
    Path('evals/consistency-root-cause-final.json').write_text(
        json.dumps(build(), ensure_ascii=False, indent=2), encoding='utf-8')
