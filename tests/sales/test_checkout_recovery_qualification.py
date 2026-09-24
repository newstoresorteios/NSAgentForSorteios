from datetime import datetime, timezone
import json

import pytest
from app.commerce.commerce_context import CommerceConversationState
from app.memory.context_resume import merge_commerce_states
from app.core.db import _merge_durable_commerce_state
from app.sales.dialogue_phase import reconcile_checkout_context
from app.sales.qualification_slots import has_bound_sale_target
from app.sales.adaptive_discovery import prepare_discovery
from app.models import IncomingMessage, AgentResult
from tests.sales.test_contextual_discovery import contextual, interpretation
from tests.sales.test_adaptive_discovery import adaptive, product, run, turn


def historical_state(**updates):
    return CommerceConversationState(**(dict(order_id='25894', order_status_group='shipped',
        terminal_order_context_cleared_for='25894', dialogue_phase='discovery',
        active_preferences={'subject_brand':'Orient'}, cart_session_id='old-cart',
        pending_action='choose_checkout_channel', checkout_draft={'address':{'zip_code':'88030300'}}) | updates))


def test_clear_persist_reload_does_not_resurrect_old_checkout():
    old=historical_state()
    clean=reconcile_checkout_context(old)
    for _ in range(3):
        # Persistence merges a richer donor; a subsequent read merges aliases.
        stored=merge_commerce_states(clean.model_dump(mode='json'),old.model_dump(mode='json'))
        loaded=_merge_durable_commerce_state(json.loads(json.dumps(stored)),old.model_dump(mode='json'))
        clean=reconcile_checkout_context(CommerceConversationState.from_payload(loaded))
        assert clean.cart_session_id is None and clean.pending_action is None
        assert not has_bound_sale_target(clean)
        assert clean.order_id=='25894'
        assert clean.checkout_draft.address.zip_code=='88030300'
        assert clean.active_preferences['subject_brand']=='Orient'


def test_new_real_cart_with_old_order_is_preserved():
    new=historical_state(cart_session_id='new-cart',cart_product_id='new-product',
        cart_context_updated_at=datetime.now(timezone.utc),dialogue_phase='checkout')
    clean=reconcile_checkout_context(new)
    loaded=CommerceConversationState.from_payload(merge_commerce_states(clean.model_dump(mode='json'),historical_state().model_dump(mode='json')))
    assert loaded.cart_session_id=='new-cart' and loaded.cart_product_id=='new-product'
    assert loaded.pending_action=='choose_checkout_channel'
    assert has_bound_sale_target(loaded)


@pytest.mark.asyncio
async def test_exact_incident_asks_budget_after_reloading_orphaned_cart(adaptive):
    state=reconcile_checkout_context(historical_state())
    state=CommerceConversationState.from_payload(merge_commerce_states(state.model_dump(mode='json'),historical_state().model_dump(mode='json')))
    async def execute(name,args):
        assert args['brand']=='Orient'
        return {'products':[dict(product(1,45,color='laranja',price=3699.99),brand='Orient')]}
    async def reply(**kwargs):
        q=kwargs['discovery_state']['contextual_question']
        return AgentResult(reply_text=q['fallback'],intent='commerce',safety_reason='commerce_clarification',response_metadata={'discovery_question':q})
    result=await prepare_discovery(interpretation=interpretation(subject={'brand':'Orient','product_type':'relógio'}),state=state,
        message=IncomingMessage(text='orient'),recent_turns=[],execute_tool=execute,generate_reply=reply)
    assert result.response_metadata['discovery_question']['slot']=='budget'
    assert '3.699' not in result.reply_text and not result.commercial_data


@pytest.mark.asyncio
async def test_single_candidate_is_not_enough_to_skip_qualification(adaptive):
    result,_=await run(interpretation(),[product(1,45)])
    assert result.response_metadata['discovery_question']['slot']=='budget'


@pytest.mark.asyncio
async def test_brand_budget_model_intent_still_allows_purpose_question(adaptive):
    first,_=await run(interpretation(),[product(1,45)])
    first.response_metadata['discovery_question']['slot']='model_intent'
    first.response_metadata['discovery_question']['adaptive']['asked']=['budget']
    i=interpretation(preferences={'budget_max':5000})
    result,_=await run(i,[],[turn(first)],text='quero uma sugestão')
    assert result.response_metadata['discovery_question']['slot']=='purchase_purpose'


@pytest.mark.asyncio
async def test_direct_request_still_skips_interview(adaptive):
    i=interpretation(stop_clarification=True)
    result,calls=await run(i,[],text='mostre as opções')
    assert result is None and not calls and i._adaptive_ready
