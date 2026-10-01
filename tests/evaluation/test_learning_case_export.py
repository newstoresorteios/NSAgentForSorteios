import pytest
from app.evaluation.learning_case_export import reviewed_incident_to_scenario


def data():
    return ({'case_key': 'immutable_turn', 'conversation_key': 'conversation',
             'customer_excerpt': 'Já respondi essa pergunta', 'correction': 'not copied',
             'metadata': {'source_response_id': 7}},
            {'case_key': 'immutable_turn', 'status': 'reviewed', 'reviewer': 'operator',
             'reviewed_at': '2026-10-01T12:00:00Z', 'history': [], 'initial_state': {},
             'requirements': ['Preserve constraints; do not repeat the answered question.']})


def test_reviewed_incident_becomes_reproducible_draft_without_copying_bad_answer():
    incident, review = data()
    scenario = reviewed_incident_to_scenario(incident, review)
    assert scenario.steps[0].input == incident['customer_excerpt']
    assert scenario.steps[0].expected.requirements == review['requirements']
    assert scenario.source_response_ids == [7]
    assert 'not copied' not in scenario.model_dump_json()


@pytest.mark.parametrize('field', ['reviewer', 'requirements', 'history', 'initial_state'])
def test_incomplete_or_unreviewed_incident_cannot_silently_become_regression(field):
    incident, review = data()
    review.pop(field)
    with pytest.raises(ValueError):
        reviewed_incident_to_scenario(incident, review)


def test_all_incidents_in_same_conversation_stay_in_same_split():
    incident, review = data()
    first = reviewed_incident_to_scenario(incident, review)
    incident['case_key'] = review['case_key'] = 'second_turn'
    second = reviewed_incident_to_scenario(incident, review)
    assert first.key != second.key and first.split == second.split


def test_unknown_conversation_cannot_be_split_as_independent_turn():
    incident, review = data()
    incident.pop('conversation_key')
    with pytest.raises(ValueError, match='conversation_required'):
        reviewed_incident_to_scenario(incident, review)


def test_offline_export_preserves_reviewer_provenance(tmp_path):
    import json
    from app.evaluation.learning_case_export import main
    incident, review = data()
    incident_path, review_path, output = (tmp_path / name for name in ('incident.json', 'review.json', 'draft.json'))
    incident_path.write_text(json.dumps(incident), encoding='utf-8')
    review_path.write_text(json.dumps(review), encoding='utf-8')
    assert main(['--incident', str(incident_path), '--review', str(review_path), '--output', str(output)]) == 0
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['status'] == 'reviewed_draft'
    assert saved['review_provenance']['reviewer'] == 'operator'
    assert saved['scenario']['source_response_ids'] == [7]
    assert len(saved['incident_hash']) == 64
