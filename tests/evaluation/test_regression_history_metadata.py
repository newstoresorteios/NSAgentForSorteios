from app.evaluation.regression_models import RegressionScenario
from app.evaluation.regression_runner import replay_history


def test_followup_scenario_preserves_actual_discovery_metadata():
    history = replay_history([], [{'input': 'Quero um relógio', 'replay': {
        'reply': 'Tem algum modelo em mente?',
        'metadata': {'discovery_question': {'topic': 'relógio', 'dimension': 'model_intent'}},
    }}])
    scenario = RegressionScenario.model_validate({
        'key': 'adaptive-followup', 'category': 'conversation', 'split': 'development',
        'history': history, 'steps': [{'input': 'Seiko', 'expected': {'requirements': ['Preserva marca']}}],
    })
    assert scenario.model_dump()['history'] == history
