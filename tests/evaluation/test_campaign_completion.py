import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from app.evaluation.regression_models import RegressionSuite, Expectations
from app.evaluation.controlled_campaign import execute_scenario, run_plan, run_campaign


def suite():
    return RegressionSuite.model_validate_json(Path('evals/priority_reference.json').read_text(encoding='utf-8'))


def test_every_critical_case_has_three_independent_runs():
    data=suite();plan=run_plan(data)
    for case in data.scenarios:
        assert [r for c,r in plan if c.key==case.key]==([1,2,3] if case.critical else [1])


@pytest.mark.asyncio
async def test_multiturn_preserves_state_and_history_without_real_commerce():
    case=next(c for c in suite().scenarios if len(c.steps)>1)
    seen=[]
    async def replay(sample,persona):
        seen.append(deepcopy(sample))
        return {'reply':'ok','next_state':{'marker':'preserved'},'simulation_state':{'cart':'isolated'}},{}
    grade=AsyncMock(return_value={'outcome':'passed','critical_errors':[]})
    report=await execute_scenario(case,SimpleNamespace(workspace_id='test'),replay_fn=replay,grade_fn=grade)
    assert report['status']=='completed'
    assert seen[1]['initial_state']=={'marker':'preserved'}
    assert seen[1]['simulation_state']=={'cart':'isolated'}
    assert seen[1]['history'][-2]['content']==case.steps[0].input
    assert seen[0]['environment']=='simulated_commerce'
    assert case.initial_state!={'marker':'preserved'}


@pytest.mark.asyncio
async def test_semantic_approval_cannot_override_objective_failure(monkeypatch):
    from app.evaluation.semantic_auditor import audit_conversation,SemanticAudit
    model=AsyncMock(return_value=SimpleNamespace(parsed=SemanticAudit(outcome='passed',findings=[],summary='ok')))
    monkeypatch.setattr('app.llm.openai_gateway.parse_structured_output',model)
    result=await audit_conversation({'reply':'ok','tools':[{'tool':'create_order'}]},
        Expectations(requirements=['Sem compra'],forbidden_tools=['create_order']))
    assert result['outcome']=='failed' and not result['auto_applied']
    assert model.call_args.kwargs['call_type']=='conversation_audit'


@pytest.mark.asyncio
async def test_audit_budget_failure_is_inconclusive(monkeypatch):
    from app.evaluation.semantic_auditor import audit_conversation
    from app.evaluation.campaign_budget import EvaluationBudgetExceeded
    monkeypatch.setattr('app.llm.openai_gateway.parse_structured_output',AsyncMock(side_effect=EvaluationBudgetExceeded('disabled')))
    result=await audit_conversation({'reply':'ok'},Expectations(requirements=['Resposta correta']))
    assert result['outcome']=='inconclusive' and result['paid_calls'] is None


@pytest.mark.asyncio
async def test_resume_never_spends_twice_and_invalidates_config(tmp_path,monkeypatch):
    from app.configuration.runtime import current_bundle
    from app.persona.persona_runtime import PersonaRuntimeConfig
    bundle=deepcopy(current_bundle());bundle['values']['evaluationCampaignPolicy']=json.dumps({'enabled':True})
    persona=PersonaRuntimeConfig(configuration_bundle=bundle)
    monkeypatch.setattr('app.persona.persona_runtime.apply_policy_overrides',lambda p,*a,**kw:p)
    data=suite();data.scenarios=data.scenarios[:1]
    data.scenarios[0].critical=False
    async def execute(case,p):
        return {'status':'completed','turns':[{'step':i,'grade':{'outcome':'passed','critical_errors':[],'judge_skipped':'offline'},'replay':{}} for i in range(len(case.steps))]}
    run=AsyncMock(side_effect=execute)
    await run_campaign(data,persona,output_dir=tmp_path,code_hash='a',execute_fn=run)
    await run_campaign(data,persona,output_dir=tmp_path,code_hash='a',execute_fn=run)
    assert run.await_count==1
    await run_campaign(data,persona,output_dir=tmp_path,code_hash='b',execute_fn=run)
    assert run.await_count==2


@pytest.mark.asyncio
async def test_incomplete_campaign_stops_and_does_not_retry(tmp_path,monkeypatch):
    from app.configuration.runtime import current_bundle
    from app.persona.persona_runtime import PersonaRuntimeConfig
    bundle=deepcopy(current_bundle());bundle['values']['evaluationCampaignPolicy']=json.dumps({'enabled':True})
    persona=PersonaRuntimeConfig(configuration_bundle=bundle)
    monkeypatch.setattr('app.persona.persona_runtime.apply_policy_overrides',lambda p,*a,**kw:p)
    run=AsyncMock(return_value={'status':'incomplete','turns':[]})
    data=suite()
    first=await run_campaign(data,persona,output_dir=tmp_path,code_hash='c',execute_fn=run)
    second=await run_campaign(data,persona,output_dir=tmp_path,code_hash='c',execute_fn=run)
    assert run.await_count==1
    assert not first['campaign_passed'] and not second['campaign_passed']
    assert first['total']==len(data.scenarios)


def test_metrics_do_not_report_failed_provider_attempt_as_free():
    from app.evaluation.sample_metrics import campaign_samples
    data=suite();case=data.scenarios[0]
    row={'status':'completed','turns':[{'grade':{'outcome':'failed','judge_skipped':'objective_failure'},
        'replay':{'runtime':{'openai_calls':[{'ok':False,'model':'m','input_tokens':0,'output_tokens':0}]}}}]}
    sample=campaign_samples(data,{case.key:row},{},{'m':{'input':1,'output':2}})[0]
    assert sample['cost_usd'] is None and sample['tokens'] is None
