from __future__ import annotations

import asyncio
from copy import deepcopy
import os

from app.configuration.runtime import bind_bundle,reset_bundle,settings_from_bundle
from app.config import get_settings
from app.evaluation import regression_repository as repository
from app.evaluation.regression_models import RegressionSuite
from app.evaluation.regression_judge import grade_turn
from app.evaluation.runner import replay_case
from app.ops.runtime_context import set_current_turn,reset_current_turn


def public_run(row):
    return {key:row.get(key) for key in ('id','suite_id','scenario_key','versions','status','next_step',
                                        'active_step','turns','created_at','finished_at')}


def replay_history(initial, turns):
    history = deepcopy(initial)
    for prior in turns:
        replay = prior.get('replay') or {}
        metadata = replay.get('metadata') or {}
        history.extend([
            {'role': 'user', 'content': prior['input']},
            {'role': 'assistant', 'content': replay.get('reply', ''), 'metadata': {
                **{key: deepcopy(metadata[key]) for key in ('handoff', 'discovery_question', 'adaptive_discovery')
                   if key in metadata},
                **({'safety_reason': replay['safety_reason']} if replay.get('safety_reason') is not None else {}),
            }},
        ])
    return history


async def run_turn(workspace,suite_id,scenario_key,run_id,step_index):
    from app.persona.persona_runtime import load_persona_runtime
    suite_row=repository.get_suite(workspace,suite_id)
    suite=RegressionSuite.model_validate(suite_row['specification'])
    scenario=next((s for s in suite.scenarios if s.key==scenario_key),None)
    if scenario is None or not 0<=step_index<len(scenario.steps):
        raise ValueError('regression_scenario_or_step_not_found')
    persona=await asyncio.to_thread(load_persona_runtime,workspace_id=workspace)
    if not persona.enabled or str(persona.workspace_id)!=workspace: raise ValueError('regression_persona_missing')
    bundle=persona.configuration_bundle
    if suite.configuration_overrides:
        from app.configuration.runtime import is_operator_setting
        from app.persona.persona_runtime import apply_policy_overrides
        fields={f['key']:f for f in bundle['fields']}
        for key in suite.configuration_overrides:
            definition=fields.get(key)
            if (not definition or definition.get('target') not in {'message','policy','setting'}
                    or (definition.get('target')=='setting' and not is_operator_setting(definition.get('attribute','')))):
                raise ValueError('regression_override_not_operator_editable')
        persona=persona.model_copy(deep=True)
        bundle=persona.configuration_bundle
        bundle['values'].update(deepcopy(suite.configuration_overrides))
        persona.runtime_configuration=deepcopy(bundle['values'])
        persona=apply_policy_overrides(persona,bundle['values'],source='regression_candidate')
    settings=settings_from_bundle(get_settings(),bundle)
    if not settings.openai_api_key: raise ValueError('regression_model_missing')
    versions={'suite':suite_row['fingerprint'],'persona':persona.persona_version_id,
              'configuration':bundle['version'],'configuration_hash':repository.fingerprint(bundle['values']),
              'model':settings.openai_model,'judge_model':bundle['values']['historyEvaluationModel'],
              'code':os.getenv('VERCEL_GIT_COMMIT_SHA') or 'working_tree',
              'deployment':os.getenv('VERCEL_URL') or 'local','environment':scenario.environment}
    row,claimed=repository.claim_turn(workspace,suite_id,scenario_key,run_id,step_index,versions)
    if not claimed: return public_run(row)
    history=replay_history(scenario.history, row['turns'])
    state=deepcopy(row['state'] if step_index else scenario.initial_state)
    case={'workspace_id':workspace,'channel':scenario.channel,'input':scenario.steps[step_index].input,
          'history':history,'initial_state':state,'recorded_at':scenario.recorded_at if step_index==0 else None,
          'evaluation_conversation_id':'evaluation:'+str(run_id).replace('-',''),
          'environment':scenario.environment,'simulation':scenario.simulation,
          'simulation_state':row.get('simulation_state')}
    tokens=bind_bundle(bundle,settings)
    outer=set_current_turn(None)
    replay={}
    try:
        replay,_=await replay_case(case,persona)
        grade=await grade_turn(scenario,scenario.steps[step_index],replay,history,persona)
    except Exception as exc:
        # Persist a safe category, never raw provider messages or credentials.
        from app.llm.openai_errors import OpenAIRateLimitGatewayError
        provider_limit = None
        if isinstance(exc, OpenAIRateLimitGatewayError):
            provider_limit = ('quota_exhausted' if exc.code == 'openai_quota_exhausted'
                              else 'rate_limit')
        grade={'outcome':'inconclusive','execution_errors':[type(exc).__name__],
               'critical_errors':[],'objective_failures':[], 'provider_limit':provider_limit}
        from app.evaluation.campaign_budget import EvaluationBudgetExceeded
        if isinstance(exc, EvaluationBudgetExceeded):
            grade['evaluation_block'] = str(exc)
    finally:
        reset_current_turn(outer)
        reset_bundle(tokens)
    result={'step':step_index,'input':case['input'], 'expected':scenario.steps[step_index].expected.model_dump(mode='json'),
            'replay':replay,'grade':grade}
    saved=repository.finish_turn(workspace,run_id,step_index,result,replay.get('next_state',state),
        replay.get('simulation_state',row.get('simulation_state')),step_index+1==len(scenario.steps))
    return public_run(saved)
