import json
from pathlib import Path

from scripts.build_october_incident_corpus import sanitize


def test_corpus_covers_every_audited_turn_without_conversation_split_leak():
    corpus = json.loads(Path('evals/october_incidents.json').read_text(encoding='utf-8'))
    conversations = corpus['conversations']
    turns = [step for convo in conversations for step in convo['steps']]
    assert len(conversations) == 23
    assert len(turns) == corpus['turn_count'] == 61
    assert {step['case_id'] for step in turns} == {str(i) for i in range(1127, 1188)}
    assert len({convo['key'] for convo in conversations}) == len(conversations)
    assert {convo['split'] for convo in conversations} == {'development', 'validation'}
    assert all(step['requirements'] and step['recorded_at'] for step in turns)
    assert all(step['candidate_result'] is None for step in turns)
    assert next(step for step in turns if step['case_id'] == '1187')['historical_responses'] == []


def test_sanitization_preserves_identifier_type_with_synthetic_document():
    assert sanitize('CPF 123.456.789-01 email teste@example.test') == 'CPF 52998224725 email [email]'
    assert sanitize('pedido 12345 modelo SSA459J1') == 'pedido 12345 modelo SSA459J1'
