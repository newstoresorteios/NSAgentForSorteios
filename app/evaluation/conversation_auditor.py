"""Manual, deterministic audit of minimized replay reports; no auto-promotion."""
from app.evaluation.regression_judge import objective_checks


def audit_replay(replay, expected):
    findings = []
    for failure in objective_checks(replay, expected):
        category = ('state' if failure.startswith('state_') else 'integration'
                    if 'tool' in failure else 'composition')
        findings.append({'category':category,'evidence':failure,
            'proposed_test':{'assertion':failure,'source':'objective_expectation'},
            'review_required':True})
    metadata = replay.get('metadata') or {}
    if replay.get('safety_reason') == 'trade_in_or_appraisal' and metadata.get('institutional_evidence'):
        findings.append({'category':'interpretation','evidence':'institutional_evidence_routed_to_appraisal',
            'proposed_test':{'assertion':'policy_question_not_appraisal'},'review_required':True})
    return {'findings':findings,'auto_applied':False,'paid_calls':0,
            'assessment':'objective_only_semantic_review_not_executed'}
