import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.evaluation.context import EvaluationContext, bind_evaluation, reset_evaluation, current_evaluation
from app.evaluation.repository import cases_from_rows
from app.evaluation.tools import evaluate_tool
from app.evaluation.judge import EvaluationVerdict, effective_outcome


def row(n, channel='whatsapp', account='store-a', conversation='thread'):
    return dict(inbound_id=n, response_id=n, conversation_id=conversation, channel=channel, account=account,
                customer_text=f'input-{n}', reply_text=f'reply-{n}', metadata={'commerce_state': {'product': n}},
                handoff_required=False, safety_reason=None)


def test_history_never_includes_future_answer_or_other_channel_account():
    rows = [row(1), row(2, channel='instagram'), row(3, account='store-b'), row(4), row(5)]
    cases = cases_from_rows(rows, 'workspace', limit=5, history_turns=10)
    target = next(c for c in cases if c['source_response_id'] == 4)
    assert target['history'] == [{'role':'user','content':'input-1'}, {'role':'assistant','content':'reply-1'}]
    assert target['initial_state'] == {'product': 1}
    assert all('reply-4' not in h['content'] and 'reply-5' not in h['content'] for h in target['history'])


@pytest.mark.asyncio
async def test_replay_forbids_commerce_writes_and_reuses_captured_results():
    context = EvaluationContext('workspace', None)
    mutate = AsyncMock(side_effect=AssertionError('must not execute'))
    assert (await evaluate_tool(context, 'create_order', {}, mutate))['error'] == 'evaluation_tool_prohibited'
    mutate.assert_not_awaited()
    read = AsyncMock(return_value={'id':'p', 'price':100})
    result = await evaluate_tool(context, 'get_product', {'product_id':'p'}, read)
    replay = EvaluationContext('workspace', None, fixtures=context.captured)
    assert await evaluate_tool(replay, 'get_product', {'product_id':'p'}, mutate) == result
    assert (await evaluate_tool(replay, 'get_product', {'product_id':'other'}, mutate))['error'] == 'evaluation_fixture_missing'
    assert replay.blocked == ['fixture_missing:get_product']


@pytest.mark.asyncio
async def test_evaluation_context_is_task_local_and_resets():
    async def run(name):
        token = bind_evaluation(EvaluationContext(name, None))
        try:
            await asyncio.sleep(0)
            return current_evaluation().workspace_id
        finally:
            reset_evaluation(token)
    assert await asyncio.gather(run('a'), run('b')) == ['a','b']
    assert current_evaluation() is None


def test_infrastructure_failure_cannot_be_reported_as_success_or_agent_failure():
    verdict = EvaluationVerdict(historical_outcome='failed', current_outcome='passed', summary='ok', findings=[], repair_instruction='')
    healthy = {'reply':'test', 'real_model_calls':1}
    assert effective_outcome(healthy, verdict) == 'passed'
    for key, value in [('error','TimeoutError'), ('blocked',['create_order']), ('integration_errors',['unavailable'])]:
        assert effective_outcome({**healthy, key:value}, verdict) == 'inconclusive'
    assert effective_outcome({**healthy,'real_model_calls':0}, verdict) == 'passed'


def test_final_technical_rejection_overrides_judge_approval():
    verdict = EvaluationVerdict(historical_outcome='passed', current_outcome='passed', summary='ok', findings=[], repair_instruction='')
    replay = {'reply':'x','real_model_calls':1,'metadata':{'final_response_validation':{'rejected_issues':['wrong_crystal']}}}
    assert effective_outcome(replay, verdict) == 'failed'


def test_history_and_state_do_not_read_customer_database():
    from app.db import load_recent_conversation_turns, load_commerce_conversation_state
    context = EvaluationContext('workspace', None, history=[{'role':'user','content':'earlier'}], state={'active_domain':'commerce'})
    token = bind_evaluation(context)
    try:
        kwargs = dict(conversation_id='x', sender_phone=None, before_inbound_id=None)
        history = load_recent_conversation_turns(**kwargs)
        state = load_commerce_conversation_state(**kwargs)
        history.clear(); state.clear()
        assert context.history and context.state
    finally:
        reset_evaluation(token)


@pytest.mark.asyncio
async def test_outbound_and_payment_boundaries_fail_before_network():
    from app.channels.brevo_client import send_brevo_reply
    from app.commerce.mercadopago_client import _mp_request
    from app.models import IncomingMessage
    token = bind_evaluation(EvaluationContext('workspace', None))
    try:
        with pytest.raises(RuntimeError, match='channel_send'):
            await send_brevo_reply(IncomingMessage(text='test'), 'test')
        with pytest.raises(RuntimeError, match='payment_provider_access'):
            await _mp_request('POST', '/v1/payments')
    finally:
        reset_evaluation(token)


@pytest.mark.asyncio
async def test_tray_direct_calls_cannot_bypass_tool_guard():
    from app.tray.tray_adapter_client import TrayAdapterClient
    token = bind_evaluation(EvaluationContext('workspace', None))
    try:
        with pytest.raises(RuntimeError, match='evaluation_side_effect_prohibited'):
            await TrayAdapterClient()._request('POST','/internal/carts')
        with pytest.raises(RuntimeError, match='evaluation_side_effect_prohibited'):
            await TrayAdapterClient()._request('GET','/internal/customers/1')
    finally:
        reset_evaluation(token)


def test_database_evaluation_connection_is_read_only(monkeypatch):
    import app.core.db as db
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    seen = {}
    def connect(*args, **kwargs):
        seen.update(kwargs)
        return Conn()
    monkeypatch.setattr(db, 'get_settings', lambda: SimpleNamespace(database_url='postgresql://example'))
    monkeypatch.setattr(db.psycopg, 'connect', connect)
    token = bind_evaluation(EvaluationContext('workspace', None))
    try:
        with db.get_conn(): pass
        assert seen['options'] == '-c default_transaction_read_only=on'
    finally:
        reset_evaluation(token)


@pytest.mark.asyncio
async def test_repair_is_replayed_with_frozen_tools_without_publishing_persona(monkeypatch):
    from app.evaluation import runner
    from app.configuration.runtime import current_bundle
    from app.persona.persona_runtime import PersonaRuntimeConfig
    from app.config import get_settings
    bundle = deepcopy(current_bundle())
    bundle['version'] = 1
    persona = PersonaRuntimeConfig(enabled=True,workspace_id='workspace',persona_version_id=1,
        configuration_bundle=bundle,runtime_configuration=deepcopy(bundle['values']))
    before = deepcopy(persona.model_dump())
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime',lambda **kw: persona)
    monkeypatch.setattr(runner,'get_settings',lambda: get_settings().model_copy(update={'openai_api_key':'test-only'}))
    monkeypatch.setattr(runner.repository,'get_case',lambda *a: {'fingerprint':'case','input':'question','historical_reply':'old'})
    monkeypatch.setattr(runner.repository,'start_run',lambda *a: True)
    saved=[]
    monkeypatch.setattr(runner.repository,'finish_run',lambda *args: saved.append(args))
    fixtures={'catalog':'frozen'}
    replay=AsyncMock(side_effect=[({'reply':'before','real_model_calls':1},fixtures),
                                  ({'reply':'after','real_model_calls':1},fixtures)])
    monkeypatch.setattr(runner,'replay_case',replay)
    verdict=lambda outcome: EvaluationVerdict(historical_outcome='failed',current_outcome=outcome,
        summary='review',findings=[],repair_instruction='Use o histórico para identificar o produto.')
    monkeypatch.setattr(runner,'judge_replay',AsyncMock(side_effect=[verdict('failed'),verdict('passed')]))
    result=await runner.evaluate_case('workspace','case','run',repair=True)
    assert result['repair']['status']=='verified_candidate'
    assert result['repair']['applied'] is False
    assert replay.await_args_list[1].kwargs['fixtures']==fixtures
    assert persona.model_dump()==before
    assert saved[-1][2]=='completed'
