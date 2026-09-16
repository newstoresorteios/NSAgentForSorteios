from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.evaluation.context import EvaluationContext,bind_evaluation,reset_evaluation
from app.evaluation.simulator import CommerceSimulator
from app.evaluation.tools import evaluate_tool
from app.evaluation.regression_judge import objective_checks
from app.evaluation.regression_models import Expectations


def simulator():
    return CommerceSimulator({'products':[{'id':'A','name':'Example','price':125.50,'available':True}]})


@pytest.mark.asyncio
async def test_commerce_simulation_never_calls_external_executor():
    sim=simulator(); context=EvaluationContext('workspace',None,simulator=sim)
    network=AsyncMock(side_effect=AssertionError('external request'))
    await evaluate_tool(context,'create_cart',{'product_id':'A','quantity':2},network)
    result=await evaluate_tool(context,'get_cart_complete',{'session_id':'SIM-SESSION'},network)
    assert result['total']==251
    assert result['items'][0]['quantity']==2
    assert all(c['simulated'] for c in context.tool_calls)
    network.assert_not_awaited()
    original=deepcopy(sim.state)
    restored=CommerceSimulator(sim.spec,original)
    restored.execute('set_cart_item_quantity',{'product_id':'A','quantity':3})
    assert restored.cart()['total']==376.5
    assert sim.cart()['total']==251


@pytest.mark.asyncio
async def test_simulation_missing_contract_is_inconclusive_not_network_fallback():
    context=EvaluationContext('workspace',None,simulator=simulator())
    network=AsyncMock(side_effect=AssertionError('external request'))
    result=await evaluate_tool(context,'unknown_future_tool',{},network)
    assert result['error']=='simulation_contract_missing'
    assert context.blocked==['simulation_contract_missing:unknown_future_tool']
    network.assert_not_awaited()


def test_simulation_state_is_not_shared_across_conversations():
    first,second=simulator(),simulator()
    first.execute('create_cart',{'product_id':'A','quantity':1})
    assert not second.state['items']


def test_evaluation_cannot_write_durable_index_even_if_database_pooler_ignores_session_options(monkeypatch):
    from app.catalog.index.catalog_index import upsert_canonical_items
    from app import db
    def forbidden():
        raise AssertionError('index evaluation opened a writable database connection')
    monkeypatch.setattr(db,'get_conn',forbidden)
    for sim in (None,simulator()):
        token=bind_evaluation(EvaluationContext('workspace',None,simulator=sim))
        try:
            assert upsert_canonical_items([object()])==0
        finally: reset_evaluation(token)


def test_evaluation_database_enforces_transaction_read_only(monkeypatch):
    from contextlib import contextmanager
    from app import db
    connection=SimpleNamespace(read_only=False)
    @contextmanager
    def connect(*args,**kwargs):
        yield connection
    monkeypatch.setattr(db.psycopg,'connect',connect)
    monkeypatch.setattr(db,'get_settings',lambda:SimpleNamespace(database_url='test'))
    token=bind_evaluation(EvaluationContext('workspace',None))
    try:
        with db.get_conn() as actual:
            assert actual.read_only is True
    finally: reset_evaluation(token)


def test_oracles_fail_wrong_money_and_unauthorized_operation():
    expected=Expectations(requirements=['respond'],forbidden_tools=['create_order'],max_price=100,
                          state_equals={'purchase_stage':'discovery'},handoff='forbidden')
    replay={'reply':'ok','tools':[{'tool':'create_order'}],'handoff':True,
            'commercial_data':{'products':[{'id':'A','price':101}]},'next_state':{'purchase_stage':'checkout'}}
    failures=objective_checks(replay,expected)
    assert set(failures)=={'forbidden_tool_called:create_order','product_budget_mismatch:A',
                           'state_mismatch:purchase_stage','unexpected_handoff'}


def test_offer_oracle_requires_offer_and_forbids_premature_transfer():
    expected=Expectations(requirements=['ask for consent'],handoff='forbidden',handoff_offer='required')
    assert objective_checks({'reply':'offer','handoff':False,'metadata':{'handoff':{'offer':True}}},expected)==[]
    assert 'unexpected_handoff' in objective_checks({'reply':'transferred','handoff':True},expected)
    assert 'handoff_offer_missing' in objective_checks({'reply':'ok','handoff':False},expected)


def test_evaluation_cannot_read_or_poison_shared_catalog_caches():
    from app.catalog.index import cache
    from app.catalog.index.snapshot import ProductSnapshotCache,ProductSnapshot,product_cache_enabled
    snapshots=ProductSnapshotCache()
    value=ProductSnapshot(product_id='A',name='production',tenant_id='test')
    snapshots.put(value,kind='detail',ttl_seconds=60)
    before=deepcopy(cache._MEMORY)
    token=bind_evaluation(EvaluationContext('workspace',None,simulator=simulator()))
    try:
        cache.store_catalog_cache('regression-poison',[{'id':'A','name':'simulated'}])
        assert cache.load_catalog_cache('regression-poison') is None
        assert snapshots.get(tenant_id='test',kind='detail',entity_id='A') is None
        snapshots.put(value.model_copy(update={'name':'poison'}),kind='detail',ttl_seconds=60)
        assert not product_cache_enabled()
        assert cache._MEMORY==before
    finally: reset_evaluation(token)
    assert snapshots.get(tenant_id='test',kind='detail',entity_id='A').name=='production'


@pytest.mark.asyncio
async def test_direct_adapter_is_blocked_even_for_reads_in_simulation():
    from app.tray.tray_adapter_client import TrayAdapterClient
    network=SimpleNamespace(request=AsyncMock(side_effect=AssertionError('network')))
    token=bind_evaluation(EvaluationContext('workspace',None,simulator=simulator()))
    try:
        with pytest.raises(RuntimeError,match='evaluation_side_effect_prohibited'):
            await TrayAdapterClient(base_url='https://example.test',token='test',http_client=network)._request('GET','/internal/products/A')
    finally: reset_evaluation(token)
    network.request.assert_not_awaited()


def test_simulated_fault_does_not_mutate_cart():
    sim=CommerceSimulator({'products':[{'id':'A','price':100}], 'faults':{'create_cart':{'status_code':503}}})
    assert sim.execute('create_cart',{'product_id':'A','quantity':1})['_simulated_fault']
    assert not sim.state['items']


@pytest.mark.asyncio
async def test_forbidden_operation_remains_critical_when_sandbox_blocks_it():
    from app.evaluation.regression_judge import grade_turn
    from app.evaluation.regression_models import RegressionStep
    step=RegressionStep(input='Just asking',expected=Expectations(requirements=['respond'],forbidden_tools=['create_order']))
    result=await grade_turn(None,step,{'reply':'','tools':[{'tool':'create_order'}],
                                    'blocked':['create_order']},[],None)
    assert result['outcome']=='failed'
    assert result['critical_errors']==['forbidden_tool_called:create_order']


def test_score_counts_unexecuted_cases_and_does_not_weight_easy_category_more():
    from app.evaluation.regression_models import RegressionSuite
    from app.evaluation.regression_score import summarize
    suite=RegressionSuite(name='x',version=1,categories=['a','b'],scenarios=[
        {'key':str(i),'category':'a' if i<9 else 'b','split':'development',
         'steps':[{'input':'x','expected':{'requirements':['answer']}}]} for i in range(10)])
    reports={str(i):{'status':'completed','turns':[{'step':0,'grade':{'outcome':'passed'}}]} for i in range(9)}
    score=summarize(suite,reports)
    assert score['score_percent']==50
    assert score['unexecuted_or_incomplete']==1
    assert not score['campaign_passed']


def test_critical_repeat_gate_counts_missing_and_failed_repetitions():
    from copy import deepcopy
    from app.evaluation.regression_models import RegressionSuite
    from app.evaluation.regression_score import summarize
    suite=RegressionSuite(name='repeats',version=1,categories=['a'],scenarios=[
        {'key':'critical','category':'a','split':'development','critical':True,
         'steps':[{'input':'x','expected':{'requirements':['answer']}}]}])
    good={'status':'completed','turns':[{'step':0,'grade':{'outcome':'passed'}}],
          'versions':{'deployment':'candidate'}}
    initial=summarize(suite,{'critical':good})
    assert initial['critical_repetition_total']==3
    assert initial['critical_repetition_percent']==33.33
    assert not initial['gates']['critical_repetitions']
    complete=summarize(suite,{'critical':good},repetitions={'critical':[deepcopy(good),deepcopy(good)]})
    assert complete['gates']['critical_repetitions']
    bad=deepcopy(good)
    bad['turns'][0]['grade']={'outcome':'failed','critical_errors':['privacy_leak']}
    failed=summarize(suite,{'critical':good},repetitions={'critical':[deepcopy(good),bad]})
    assert not failed['gates']['no_critical_errors']
    assert not failed['gates']['critical_repetitions']


@pytest.mark.asyncio
async def test_multiturn_uses_generated_reply_and_new_state_not_golden_history(monkeypatch):
    from app.evaluation import regression_runner as runner
    from app.evaluation.regression_models import RegressionSuite
    import app.persona.persona_runtime as persona_module
    suite=RegressionSuite(name='x',version=1,categories=['a'],scenarios=[{'key':'one','category':'a',
        'split':'development','initial_state':{'active_product':{'product_id':'OLD'}},
        'steps':[{'input':'first','expected':{'requirements':['a']}},{'input':'second','expected':{'requirements':['b']}}]}])
    persona=SimpleNamespace(enabled=True,workspace_id='w',persona_version_id=1,
        configuration_bundle={'version':1,'values':{'historyEvaluationModel':'judge'}})
    monkeypatch.setattr(persona_module,'load_persona_runtime',lambda **_:persona)
    monkeypatch.setattr(runner,'settings_from_bundle',lambda *_:SimpleNamespace(openai_api_key='test',openai_model='test'))
    monkeypatch.setattr(runner,'bind_bundle',lambda *_:None)
    monkeypatch.setattr(runner,'reset_bundle',lambda *_:None)
    monkeypatch.setattr(runner.repository,'get_suite',lambda *_:{'specification':suite.model_dump(),'fingerprint':'hash'})
    saved={'turns':[{'input':'first','replay':{'reply':'ACTUAL GENERATED',
           'metadata':{'handoff':{'offer':True,'required':False},'unrelated':'not copied'}}}],
           'state':{'active_product':{'product_id':'NEW'}},'simulation_state':{'items':[{'product_id':'NEW'}]}}
    monkeypatch.setattr(runner.repository,'claim_turn',lambda *_:(saved,True))
    replay=AsyncMock(return_value=({'reply':'second answer','next_state':saved['state']},{}))
    monkeypatch.setattr(runner,'replay_case',replay)
    monkeypatch.setattr(runner,'grade_turn',AsyncMock(return_value={'outcome':'passed'}))
    monkeypatch.setattr(runner.repository,'finish_turn',lambda *args:{'id':'r','turns':[args[3]],'status':'completed'})
    result=await runner.run_turn('w','s','one','r',1)
    case=replay.call_args.args[0]
    assert case['history']==[{'role':'user','content':'first'},{'role':'assistant','content':'ACTUAL GENERATED',
                              'metadata':{'handoff':{'offer':True,'required':False}}}]
    assert case['initial_state']['active_product']['product_id']=='NEW'
    assert case['simulation_state']==saved['simulation_state']
    assert case['evaluation_conversation_id']=='evaluation:r'
    assert result['status']=='completed'
    replay.reset_mock()
    monkeypatch.setattr(runner.repository,'claim_turn',lambda *_:({'id':'r','turns':[]},False))
    await runner.run_turn('w','s','one','r',1)
    replay.assert_not_awaited()
