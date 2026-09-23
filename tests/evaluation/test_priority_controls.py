import json
from copy import deepcopy
from pathlib import Path
import pytest
from app.evaluation.campaign_budget import reservation, EvaluationBudgetExceeded
from app.evaluation.comparison import compare_samples
from app.llm.compact_context import compact_payload
from app.evaluation.regression_models import RegressionSuite

def test_frozen_reference_has_distinct_holdout_and_full_expectations():
    suite=RegressionSuite.model_validate_json(Path('evals/priority_reference.json').read_text(encoding='utf-8'))
    assert len(suite.scenarios)>=96
    assert sum(s.split=='validation' for s in suite.scenarios)>=20
    assert all(sum(s.category==c for s in suite.scenarios)>=suite.minimum_cases_per_category for c in suite.categories)
    assert {'development','validation'}=={s.split for s in suite.scenarios}
    assert all(s.environment=='simulated_commerce' for s in suite.scenarios)
    assert all(step.expected.requirements for s in suite.scenarios for step in s.steps)

def test_compaction_is_lossless_for_critical_state():
    facts={'product_id':'a','variant_id':'steel','postal_code':'88030300','description':'details '*300}
    payload={'state':deepcopy(facts),'working_memory':deepcopy(facts),'facts':deepcopy(facts)}
    compact=compact_payload(payload)
    def expand(v):
        if isinstance(v,dict):
            if set(v)=={'_context_ref'}:return compact['context_evidence'][v['_context_ref']]
            return {k:expand(x) for k,x in v.items()}
        if isinstance(v,list):return [expand(x) for x in v]
        return v
    assert expand(compact['context'])==payload
    assert len(json.dumps(compact))<len(json.dumps(payload))*.7

@pytest.mark.parametrize('policy', [{},{'enabled':False},{'enabled':True}])
def test_paid_calls_block_without_explicit_complete_budget(policy):
    with pytest.raises(EvaluationBudgetExceeded):reservation(policy,model='test',messages=[],output_limit=100)

def test_reserves_uncached_upper_bound_and_rejects_missing_rates():
    policy={'enabled':True,'campaign_id':'test','price_version':'v1','max_calls':3,'max_tokens':80000,
            'max_cost_usd':1,'max_input_tokens_per_call':20000,'prices':{'test':{'input':1,'output':2}}}
    tokens,cost=reservation(policy,model='test',messages=[],output_limit=1000)
    assert tokens==21000 and float(cost)==.022
    with pytest.raises(EvaluationBudgetExceeded):reservation(policy,model='unknown',messages=[],output_limit=1000)
    with pytest.raises(EvaluationBudgetExceeded):reservation(policy,model='test',messages=[{'image_url':'x'}],output_limit=1000)

def test_comparison_rejects_missing_or_different_samples():
    row={'case_id':'a','repetition':1,'split':'validation','fixture_hash':'f','persona_hash':'p','code_hash':'c','complete':True,'passed':True,'latency_ms':100}
    assert compare_samples([row],[row])['quality_non_regression']
    assert not compare_samples([row],[])['quality_non_regression']
    assert not compare_samples([row],[{**row,'fixture_hash':'other'}])['comparable']
    assert not compare_samples([row],[row])['release_approved']
    assert compare_samples([row],[row])['candidate']['cost_usd'] is None
    assert not compare_samples([row],[row])['cost_within_limit']
    baseline={**row,'cost_usd':.01}
    candidate={**row,'cost_usd':.02}
    assert not compare_samples([baseline],[candidate])['cost_within_limit']
    assert compare_samples([baseline],[candidate],max_cost_ratio=2)['cost_within_limit']
