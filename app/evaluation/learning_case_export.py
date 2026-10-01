"""Convert reviewed immutable incidents into draft regression specifications."""
from __future__ import annotations

import hashlib
from app.evaluation.regression_models import RegressionScenario


def reviewed_incident_to_scenario(incident: dict, review: dict) -> RegressionScenario:
    """A correction is not its own judge: reviewed criteria must be provided separately.

    This function produces a draft, without activating extensions or executing tools.
    The review is tied to the immutable incident key and must supply its prior context.
    """
    identity = str(incident.get('case_key') or '')
    if not identity or review.get('case_key') != identity:
        raise ValueError('incident_review_identity_mismatch')
    if review.get('status') != 'reviewed' or not review.get('reviewer') or not review.get('reviewed_at'):
        raise ValueError('incident_review_required')
    if not review.get('requirements'):
        raise ValueError('independent_acceptance_criteria_required')
    if 'history' not in review or 'initial_state' not in review:
        raise ValueError('incident_context_required')
    conversation = str(incident.get('conversation_key') or '').strip()
    if not conversation:
        raise ValueError('incident_conversation_required_for_split')
    group = hashlib.sha256(conversation.encode()).hexdigest()
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    source = (incident.get('metadata') or {}).get('source_response_id')
    return RegressionScenario(
        key='learned_' + key,
        category=str(review.get('category') or 'learned_incident'),
        split='validation' if int(group[:8], 16) % 4 == 0 else 'development',
        critical=bool(review.get('critical')), source_response_ids=[source] if source is not None else [],
        channel=review.get('channel', 'whatsapp'), history=review['history'],
        initial_state=review['initial_state'], recorded_at=review.get('recorded_at'),
        simulation=review.get('simulation') or {}, environment='simulated_commerce',
        steps=[{'input': incident['customer_excerpt'], 'expected': {
            'requirements': review['requirements'],
            'forbidden_tools': review.get('forbidden_tools') or [],
            'state_equals': review.get('state_equals') or {},
        }}],
    )


def main(argv=None) -> int:
    """Offline entry point; writes a reviewed draft, never publishes or runs it."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incident', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    incident = json.loads(args.incident.read_text(encoding='utf-8'))
    review = json.loads(args.review.read_text(encoding='utf-8'))
    scenario = reviewed_incident_to_scenario(incident, review)
    output = {
        'status': 'reviewed_draft',
        'scenario': scenario.model_dump(mode='json'),
        'review_provenance': {key: review[key] for key in ('case_key', 'reviewer', 'reviewed_at')},
        'incident_hash': hashlib.sha256(json.dumps(incident, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
