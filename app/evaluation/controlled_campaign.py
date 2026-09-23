"""Sequential, resumable evaluation against isolated commerce fixtures.

Each independent repetition has fresh state. Interrupted cases are not replayed
automatically: their provider cost may already have been incurred.
"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import time


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


def run_plan(suite):
    return [(case, repetition) for case in suite.scenarios
            for repetition in range(1, 4 if case.critical else 2)]


async def execute_scenario(case, persona, *, replay_fn=None, grade_fn=None):
    from app.evaluation.runner import replay_case
    from app.evaluation.regression_judge import grade_turn
    replay_fn,grade_fn=replay_fn or replay_case,grade_fn or grade_turn
    history=deepcopy(case.history);state=deepcopy(case.initial_state);simulation_state=None
    turns=[];started=time.perf_counter()
    for index,step in enumerate(case.steps):
        sample={'workspace_id':str(persona.workspace_id),'channel':case.channel,
                'history':deepcopy(history),'input':step.input,'initial_state':deepcopy(state),
                'recorded_at':case.recorded_at if index==0 else None,
                'environment':'simulated_commerce','simulation':deepcopy(case.simulation),
                'simulation_state':deepcopy(simulation_state)}
        replay,_=await replay_fn(sample,persona)
        grade=await grade_fn(case,step,replay,history,persona)
        turns.append({'step':index,'input':step.input,'replay':replay,'grade':grade})
        if replay.get('error') or grade.get('outcome')=='inconclusive':
            break
        state=deepcopy(replay.get('next_state',state))
        simulation_state=deepcopy(replay.get('simulation_state'))
        history.extend([{'role':'user','content':step.input},
                        {'role':'assistant','content':replay.get('reply',''),
                         'metadata':{'handoff':deepcopy((replay.get('metadata') or {}).get('handoff') or {})}}])
    complete=len(turns)==len(case.steps) and all(t['grade'].get('outcome')!='inconclusive' for t in turns)
    return {'status':'completed' if complete else 'incomplete','turns':turns,
            'elapsed_ms':round((time.perf_counter()-started)*1000,2)}


async def run_campaign(suite, persona, *, output_dir, code_hash, overrides=None, execute_fn=None):
    from app.configuration.runtime import bind_bundle,reset_bundle,settings_from_bundle,is_operator_setting
    from app.config import get_settings
    from app.persona.persona_runtime import apply_policy_overrides
    from app.evaluation.regression_score import summarize
    candidate=persona.model_copy(deep=True)
    bundle=candidate.configuration_bundle
    fields={f['key']:f for f in bundle['fields']}
    for key in overrides or {}:
        field=fields.get(key,{})
        if field.get('target') not in {'message','policy','setting'} or (
            field.get('target')=='setting' and not is_operator_setting(field.get('attribute',''))):
            raise ValueError('campaign_override_not_operator_editable')
        if key=='evaluationCampaignPolicy':
            raise ValueError('campaign_budget_override_prohibited')
    bundle['values'].update(deepcopy(overrides or {}))
    candidate.runtime_configuration=deepcopy(bundle['values'])
    candidate=apply_policy_overrides(candidate,bundle['values'],source='controlled_campaign')
    policy=bundle['values'].get('evaluationCampaignPolicy','{}')
    policy=json.loads(policy) if isinstance(policy,str) else policy
    if not policy.get('enabled'):
        raise ValueError('evaluation_campaign_disabled')
    if any(c.environment!='simulated_commerce' for c in suite.scenarios):
        raise ValueError('controlled_campaign_requires_simulated_commerce')
    settings=settings_from_bundle(get_settings(),bundle)
    versions={'suite':fingerprint(suite.model_dump()),'persona':fingerprint(persona.flow_params_dict()),
              'configuration_hash':fingerprint(bundle['values']),'model':settings.openai_model,
              'judge_model':bundle['values']['historyEvaluationModel'],'deployment':code_hash}
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=True)
    reports={};repeats={};binding=bind_bundle(bundle,settings)
    try:
        for case,repetition in run_plan(suite):
            identity={**versions,'case':case.key,'repetition':repetition}
            path=root/(fingerprint(identity)+'.json')
            if path.exists():
                row=json.loads(path.read_text(encoding='utf-8'))
                if row.get('identity')!=identity or row.get('status')!='completed':
                    break
            else:
                # Exclusive marker prevents two runners spending for one sample.
                with path.open('x',encoding='utf-8') as out:
                    json.dump({'identity':identity,'status':'running'},out)
                try:
                    row=await (execute_fn or execute_scenario)(case,candidate)
                except Exception as exc:
                    row={'status':'incomplete','turns':[],'error_type':type(exc).__name__}
                row.update(identity=identity,versions=versions)
                tmp=path.with_suffix('.tmp')
                tmp.write_text(json.dumps(row,ensure_ascii=False,default=str),encoding='utf-8')
                tmp.replace(path)
            if repetition==1:reports[case.key]=row
            else:repeats.setdefault(case.key,[]).append(row)
            if row['status']!='completed':break
    finally:
        reset_bundle(binding)
    score=summarize(suite,reports,repetitions=repeats)
    score.update(versions=versions,automatic_promotion=False,
                 comparison_limits=policy.get('comparison_limits',{}))
    from app.evaluation.sample_metrics import campaign_samples
    score['samples']=campaign_samples(suite,reports,repeats,policy.get('prices',{}))
    target=root/('summary-'+fingerprint(versions)+'.json')
    target.write_text(json.dumps(score,indent=2),encoding='utf-8')
    return score
