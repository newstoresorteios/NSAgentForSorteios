"""Evidence gate: collection, mocks and missing reviews are not production proof."""
from __future__ import annotations


def assess_release_evidence(manifest, results, required_ids, *, production_required=True):
    required = set(required_ids)
    by_id = {}
    duplicates = set()
    for row in results:
        key = row.get('case_id')
        if key in by_id:
            duplicates.add(key)
        by_id[key] = row
    missing = sorted(required - by_id.keys())
    failed, unverified = [], []
    for key in sorted(required & by_id.keys()):
        row = by_id[key]
        if row.get('outcome') == 'failed':
            failed.append(key)
        elif (row.get('outcome') != 'passed' or row.get('review_status') != 'reviewed'
              or not row.get('observed_reply_hash') or not row.get('criteria_results')
              or not all(c.get('passed') is True and c.get('evidence') for c in row['criteria_results'])
              or (production_required and row.get('execution_mode') != 'real_model_isolated')):
            unverified.append(key)
    reasons = []
    if not required:
        reasons.append('empty_required_corpus')
    if missing:
        reasons.append('missing_cases')
    if failed:
        reasons.append('failed_cases')
    if unverified:
        reasons.append('unverified_cases')
    if duplicates:
        reasons.append('duplicate_cases')
    if manifest.get('reproducible') is not True:
        reasons.append('incomplete_version_manifest')
    return {'approved': not reasons, 'scope': 'production' if production_required else 'offline',
            'required': len(required), 'missing': missing, 'failed': failed,
            'unverified': unverified, 'duplicate': sorted(duplicates), 'reasons': reasons}
