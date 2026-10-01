from copy import deepcopy
from app.evaluation.regression_models import RegressionSuite
from app.evaluation.regression_score import summarize


def test_catalog_fixtures_may_differ_between_scenarios_but_not_repetitions():
    suite = RegressionSuite(name='scope', version=1, categories=['catalog'], scenarios=[
        {'key': key, 'category': 'catalog', 'split': 'development', 'critical': True,
         'steps': [{'input': 'availability', 'expected': {'requirements': ['query catalog']}}]}
        for key in ('available', 'unavailable')])
    reports = {key: {'status': 'completed', 'versions': {'deployment': 'same', 'catalog_hash': key},
                     'turns': [{'step': 0, 'grade': {'outcome': 'passed'}}]}
               for key in ('available', 'unavailable')}
    repeats = {key: [deepcopy(row), deepcopy(row)] for key, row in reports.items()}
    result = summarize(suite, reports, repetitions=repeats)
    assert result['gates']['single_candidate']
    assert result['gates']['stable_scenario_fixtures']
    repeats['available'][0]['versions']['catalog_hash'] = 'changed_price'
    result = summarize(suite, reports, repetitions=repeats)
    assert result['gates']['single_candidate']
    assert not result['gates']['stable_scenario_fixtures']
