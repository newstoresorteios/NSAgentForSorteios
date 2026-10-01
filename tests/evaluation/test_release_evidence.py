from app.evaluation.release_evidence import assess_release_evidence


def row(**overrides):
    return dict(case_id='1', outcome='passed', review_status='reviewed',
                observed_reply_hash='sha', execution_mode='real_model_isolated',
                criteria_results=[{'passed': True, 'evidence': 'tool confirms availability'}], **overrides)


def test_collection_or_mocked_approval_is_not_production_proof():
    result = row()
    result['execution_mode'] = 'offline_interpretation_fixture'
    assert not assess_release_evidence({'reproducible': True}, [result], ['1'])['approved']
    assert assess_release_evidence({'reproducible': True}, [result], ['1'], production_required=False)['approved']


def test_partial_coverage_duplicate_and_missing_versions_block_release():
    result = assess_release_evidence({}, [row(), row()], ['1', '2'])
    assert set(result['reasons']) == {'duplicate_cases', 'missing_cases', 'incomplete_version_manifest'}


def test_criteria_failure_cannot_be_overridden_by_overall_pass():
    result = row()
    result['criteria_results'][0]['passed'] = False
    assert not assess_release_evidence({'reproducible': True}, [result], ['1'])['approved']


def test_complete_reviewed_execution_passes_only_matching_scope():
    assert assess_release_evidence({'reproducible': True}, [row()], ['1'])['approved']
    assert not assess_release_evidence({'reproducible': True}, [], [])['approved']
