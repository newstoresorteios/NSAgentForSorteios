import pytest
from app.evaluation.campaign_budget import contains_media_input, EvaluationBudgetExceeded
from app.evaluation.context import EvaluationContext, bind_evaluation, reset_evaluation
from scripts.build_launch_conversation_suite import build, MUTATIONS


def test_text_catalog_image_urls_do_not_trigger_media_budget():
    assert not contains_media_input([{'role': 'user', 'content': '{"primary_image_url":"https://example.test/a.jpg"}'}])
    assert not contains_media_input({'response_format': {'schema': {'properties': {'image_url': {'type': 'string'}}}}})
    assert contains_media_input([{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.test/a.jpg'}]}])
    assert contains_media_input([{'image_url': 'https://example.test/a.jpg'}])


def test_campaign_is_bounded_and_readonly():
    suite = build()
    assert sum(len(s['steps']) for s in suite['scenarios']) == 14
    assert all(s['environment'] == 'live_readonly' for s in suite['scenarios'])
    assert all(set(MUTATIONS) <= set(t['expected']['forbidden_tools']) for s in suite['scenarios'] for t in s['steps'])


def test_text_reservation_keeps_upper_bound_and_input_ceiling():
    import json
    from app.evaluation.campaign_budget import reservation
    budget = json.loads(build()['configuration_overrides']['evaluationCampaignPolicy'])
    messages = [{'role': 'user', 'content': 'Olá relógio ' * 100}]
    tokens, cost = reservation(budget, model='gpt-5.4-mini', messages=messages, output_limit=6000)
    encoded = len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
    assert tokens == encoded + 16384 + 6000
    assert cost > 0
    with pytest.raises(EvaluationBudgetExceeded, match='bound_missing_or_exceeded'):
        reservation({**budget, 'max_input_tokens_per_call': encoded},
                    model='gpt-5.4-mini', messages=messages, output_limit=6000)


def test_swallowed_budget_error_remains_in_evaluation_evidence():
    context = EvaluationContext('test', None)
    token = bind_evaluation(context)
    try:
        with pytest.raises(EvaluationBudgetExceeded):
            raise EvaluationBudgetExceeded('evaluation_campaign_disabled')
        assert context.budget_errors == ['evaluation_campaign_disabled']
    finally:
        reset_evaluation(token)
