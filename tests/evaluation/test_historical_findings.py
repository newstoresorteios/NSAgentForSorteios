import asyncio
from unittest.mock import AsyncMock

import pytest

from app.catalog.retrieval.availability import unavailable_product_reply
from app.evaluation.context import EvaluationContext
from app.evaluation.tools import evaluate_tool
from app.evaluation.judge import EvaluationVerdict, effective_outcome


def test_inactive_product_lead_time_does_not_promise_order_or_business_days():
    reply = unavailable_product_reply([{'id':'1', 'available':'0', 'upon_request':'0',
        'availability_days':'30', 'availability':'Disponível em 30 dias úteis'}])
    assert 'indisponível para compra' in reply
    assert '“Disponível em 30 dias úteis”' in reply
    assert 'sob encomenda' not in reply
    numeric_only = unavailable_product_reply([{'id':'1','available':'0','availability_days':'30'}])
    assert '30' not in numeric_only


def test_unrequested_write_attempt_is_a_failure_even_when_sandbox_blocks():
    verdict = EvaluationVerdict(historical_outcome='failed', current_outcome='failed', summary='intent error',
        repair_instruction='', findings=[dict(stage='understanding',severity='critical',evidence='create_cart',
            explanation='customer only asked a feature',suggested_fix='fix purchase selection')])
    assert effective_outcome({'reply':'x','blocked':['create_cart']}, verdict) == 'failed'


@pytest.mark.asyncio
async def test_concurrent_reads_share_a_hard_tool_budget():
    context = EvaluationContext('workspace',None,max_tool_calls=2)
    async def read():
        await asyncio.sleep(0)
        return {'id':'p'}
    execute = AsyncMock(side_effect=read)
    results = await asyncio.gather(*(evaluate_tool(context,'get_product',{'product_id':str(i)},execute) for i in range(6)))
    assert execute.await_count == 2
    assert sum('error' in result for result in results) == 4


def test_replay_preserves_session_age_at_original_message():
    from datetime import datetime, timezone
    from app.evaluation.runner import replay_state
    from app.sales.dialogue_phase import is_browse_idle
    from app.commerce.commerce_context import CommerceConversationState
    case = {'recorded_at':'2026-09-12T16:01:50Z','initial_state':{
        'last_browse_at':'2026-09-12T16:01:21Z','active_preferences':{'subject_brand':'Tissot'}}}
    now = datetime(2026,9,16,tzinfo=timezone.utc)
    state = CommerceConversationState.model_validate(replay_state(case,now=now))
    assert (now-state.last_browse_at).total_seconds() == 29
    assert not is_browse_idle(state,now=now,idle_seconds=60)
    assert is_browse_idle(state,now=now,idle_seconds=20)
    assert case['initial_state']['last_browse_at'].startswith('2026-09-12')


def test_live_availability_note_survives_authority_and_does_not_imply_sale():
    from app.verify.fact_authority import authorize_products_for_responder
    from app.verify.factual_validator import validate_factual_response
    from app.llm.agent_contracts import build_agent_decision
    from app.models import AgentResult, IncomingMessage
    product = {'id':'1','name':'Modelo de teste','stock':38,'available':'0','upon_request':'0',
        'availability':'Disponível em 30 dias úteis','availability_days':'30',
        'description':'Movimento automático com corda manual','_revalidated':True,'_factual_source':'tray_live'}
    product['commercial_availability'] = {'availability_state':'unavailable','stock':38,'has_stock':True}
    products,_ = authorize_products_for_responder([product])
    assert products[0]['availability'] == product['availability']
    assert products[0]['description'] == product['description']
    # The first factual pass sees the full retrieval document, before the
    # authority projection removes derived availability metadata.
    products[0]['commercial_availability'] = product['commercial_availability']
    result = AgentResult(reply_text=unavailable_product_reply(products),intent='commerce',
        commercial_data={'products':products},response_metadata={'domain':'commerce','used_tray':True})
    decision=build_agent_decision(IncomingMessage(text='consulta'),result,openai_call_count=1)
    report=validate_factual_response(result,decision=decision,mode='enforce')
    assert report.valid, report.violations
    result.reply_text = 'Está disponível para compra agora.'
    assert not validate_factual_response(result,decision=decision,mode='enforce').valid


def test_automatic_with_auxiliary_manual_winding_does_not_become_mismatch():
    from app.catalog.specs.requirements import feature_evidence
    assert feature_evidence({'movement':'Automático com corda manual'},{'mechanism':'automatic'})['status']=='matched'
    assert feature_evidence({'movement':'corda manual'},{'mechanism':'automatic'})['status']=='mismatch'
    assert feature_evidence({'movement':'Automático ou quartzo'},{'mechanism':'automatic'})['status']=='mismatch'


def test_rejected_unrepaired_answer_cannot_pass_historical_judge():
    verdict = EvaluationVerdict(historical_outcome='passed',current_outcome='passed',summary='ok',findings=[],repair_instruction='')
    replay={'reply':'Se quiser eu busco','real_model_calls':3,'metadata':{'response_critique':{
        'review_status':'unavailable','verdicts':[{'pass_check':False}],'applied_factual_fallback':False}}}
    assert effective_outcome(replay,verdict)=='failed'


def test_memory_generation_schema_has_no_open_objects_or_untyped_arrays():
    from app.memory.memory_models import AgentTurnEnvelope, MemoryProposal
    from openai.lib._pydantic import to_strict_json_schema
    schema = to_strict_json_schema(AgentTurnEnvelope)
    def inspect(node):
        if not isinstance(node,dict):
            return
        if node.get('type') == 'object':
            assert node.get('additionalProperties') is False
        if node.get('type') == 'array':
            assert node.get('items'), node
        for value in node.values():
            if isinstance(value,dict): inspect(value)
            elif isinstance(value,list):
                for child in value: inspect(child)
    inspect(schema)
    proposal = MemoryProposal(value='{"color":"preto"}')
    assert proposal.value == {'color':'preto'}


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [0, None])
@pytest.mark.parametrize('upon_request', ['0', '1'])
async def test_technical_match_with_missing_live_price_keeps_partial_evidence(price, upon_request):
    from types import SimpleNamespace
    from app.catalog.retrieval.technical import retrieve_technical_products
    from app.models import SalesInterpretation
    from app.verify.response_critique import _handle_unavailable_review, CritiqueLoopReport
    from app.configuration.runtime import bind_bundle, current_bundle, reset_bundle
    interpretation = SalesInterpretation.model_validate({'domain':'commerce','goal':'find',
        'references_previous_context':False,'needs_clarification':False,'confidence':1,
        'preferences':{'mechanism':'automatic','crystal':'sapphire','budget_max':2500}})
    product = {'id':'test','name':'Modelo de teste','price':price,'available':True,'upon_request':upon_request,
               'description':'Movimento automático e cristal de safira'}
    session = SimpleNamespace(interpretation=interpretation,
        candidates=[{**product,'price':2000}], excluded_ids=set(),
        retrieval_plan=SimpleNamespace(candidate_limit=20,mode='recommendation'),
        list_query_extras=lambda _: {},search_products=AsyncMock(return_value={'products':[]}),
        execute_tool=AsyncMock(return_value=product))
    result = await retrieve_technical_products(session)
    assert result.commercial_data['products'] == []
    assert 'Modelo de teste' in result.reply_text
    assert ('sem preço válido' if upon_request == '1' else 'não trouxe um preço válido') in result.reply_text
    if upon_request == '1':
        assert 'disponibilidade sob consulta' in result.reply_text
    assert result.response_metadata['technical_evidence'][-1]['commercial']['price_status'] == 'missing'
    original = result.reply_text
    result.reply_text = 'Se quiser eu busco.'
    result.response_metadata['interpretation'] = interpretation.model_dump(mode='json')
    bundle = current_bundle()
    token = bind_bundle({**bundle,'values':{**bundle['values'],'critiqueUnavailableAction':'grounded_fallback'}},None)
    try:
        report = CritiqueLoopReport(mode='enforce')
        corrected = _handle_unavailable_review(result,report,'repair_budget_unavailable')
        assert corrected.reply_text == original
        assert report.applied_factual_fallback
        assert not corrected.handoff_required
    finally:
        reset_bundle(token)
