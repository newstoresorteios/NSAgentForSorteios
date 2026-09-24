"""Export selected synthetic conversation evidence, without credentials or raw traces."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ['consistency-a88e8d4', 'consistency-technical-a88e8d4', 'consistency-expanded-a88e8d4']


def export():
    turns = []
    for folder in FOLDERS:
        for path in (ROOT / 'evals/results' / folder).glob('*.json'):
            report = json.loads(path.read_text(encoding='utf-8'))
            for turn in report.get('turns', []):
                replay = turn.get('replay') or {}
                metadata = replay.get('metadata') or {}
                turns.append({
                    'scenario': report['scenario_key'], 'run_id': report['id'],
                    'step': turn['step'], 'input': turn['input'], 'reply': replay.get('reply'),
                    'automated_outcome': turn['grade'].get('outcome'),
                    'automated_summary': turn['grade'].get('summary'),
                    'safety_reason': replay.get('safety_reason'),
                    'budget_errors': replay.get('budget_errors'),
                    'preferences': replay.get('next_state', {}).get('active_preferences'),
                    'adaptive_discovery': metadata.get('adaptive_discovery'),
                    'response_source': metadata.get('response_source'),
                    'double_check': metadata.get('double_check'),
                })
    output = ROOT / 'docs/audits/2026-09-23/consistency-a88e8d4-live-evidence.json'
    output.write_text(json.dumps({
        'code': 'a88e8d4', 'campaign': 'consistency-a88e8d4-new5-20260924',
        'authorized_additional_usd': 5,
        'reserved_usd': 4.26891675,
        'reserved_calls': 51,
        'billing_note': 'Conservative reservation, not actual provider invoice. Campaign stopped.',
        'manual_review': {
            'screenshot_orient_2500:2': 'False negative: Orient is preserved in state; repetition in reply is unnecessary.',
            'screenshot_orient_2500:3': 'Qualification stops after budget; no confirmed recommendation. Production counterpart additionally vetoed by double-check.',
            'technical_and_reset:1': 'Technical criteria preserved in state but omitted from explanation; use-purpose polluted customer name.',
            'technical_and_reset:2': 'Wrong budget basis: says over 3000 while displaying Pix 2889.99. Requested technical enumeration missing.',
            'technical_and_reset:3': 'Restart reply correct, but old subject_reference remains in state.',
            'technical_and_reset:4': 'Automatic approval insufficient: delivered generic double-check failure.',
            'orient_identity_negative:0': 'Identity correct; requested crystal, size and delivery details omitted.',
            'orient_identity_negative:1': 'Budget/color corrected; crystal and tomorrow delivery left unanswered.',
            'qualification_changes:0': 'Explicit classic style lost before filtering; diver offered.',
            'qualification_changes:2': 'Movement updated, but Orient silently removed and Bulova offered.',
            'qualification_changes:3': 'Stop-questions respected, but mechanism remains quartz despite any movement; lost-brand state persists.',
            'qualification_changes:4': 'Human handoff intention honored; actual customer-channel handoff not exercised.',
        },
        'environment': 'isolated deployment with production OpenAI/configuration and read-only Tray',
        'limitations': ['No customer channel delivery', 'No commercial mutations',
                        'Automated outcomes require manual review; do not use as approval rate'],
        'turns': turns,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), 'turns': len(turns)}))


if __name__ == '__main__':
    export()
