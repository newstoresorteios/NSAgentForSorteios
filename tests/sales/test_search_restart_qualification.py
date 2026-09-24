import pytest
from app.models import SalesInterpretation
from app.commerce.commerce_context import CommerceConversationState
from app.sales.dialogue_phase import is_fresh_commerce_start, is_bare_commerce_restart, reset_browse_memory_keep_orders
from app.memory.contact_preference_memory import rehydrate_interpretation_from_memories
from tests.memory.test_contact_preference_memory import _catalog_memories
from app.sales.qualification_enrichment import apply_qualification_answer, qualification_known
from app.catalog.retrieval.hard_filter import hard_filter_products
from tests.sales.test_contextual_discovery import contextual, interpretation
from tests.sales.test_adaptive_discovery import adaptive, run, product, turn


@pytest.mark.parametrize('text', ['Reinicia a conversa', 'Começa de novo', 'Recomece', 'Vamos começar do zero'])
def test_restart_then_brand_does_not_restore_old_requirements(text):
    assert is_fresh_commerce_start(text) and is_bare_commerce_restart(text)
    state=CommerceConversationState(active_preferences=dict(color='azul',style='open heart',budget_max=2500,
        mechanism='automatic',crystal='sapphire',attributes=['required_strap_material:aco','qual:name:João'],
        qualification_slots={'customer_name':'João','occasion':'social'}), order_id='123')
    state=reset_browse_memory_keep_orders(state)
    assert state.order_id=='123'
    assert state.active_preferences['attributes']==['qual:name:João']
    assert not any(state.active_preferences.get(k) for k in ['color','style','budget_max','mechanism','crystal'])
    i=interpretation(subject={'brand':'Orient'},references_previous_context=True)
    updated, filled=rehydrate_interpretation_from_memories(i,_catalog_memories(),message_text='Orient')
    assert not filled
    assert updated.subject.brand=='Orient'
    assert updated.preferences.budget_max is None and updated.preferences.color is None


def test_reset_does_not_swallow_new_request_or_negation():
    assert not is_fresh_commerce_start('não reinicia a conversa')
    assert not is_bare_commerce_restart('começa de novo, quero um Orient')


def test_qualification_answers_are_distinct_and_filter_catalog():
    i=interpretation()
    apply_qualification_answer(i,'presente','purchase_purpose')
    assert qualification_known(i)=={'purchase_purpose'}
    apply_qualification_answer(i,'Feminino','gender')
    apply_qualification_answer(i,'Mergulho','style')
    apply_qualification_answer(i,'Quartzo','mechanism')
    assert {'purchase_purpose','gender','style','mechanism'} <= qualification_known(i)
    rows=[dict(product(1,38),gender='Feminino',style='Mergulho',mechanism='quartz'),
          dict(product(2,38),gender='Masculino',style='Mergulho',mechanism='quartz'),
          dict(product(3,38),gender='Feminino',style='Dress',mechanism='quartz')]
    assert [p['id'] for p in hard_filter_products(rows,i,mode='recommendation')]==['1']


@pytest.mark.asyncio
async def test_style_facet_continues_after_three_questions(adaptive):
    rows=[dict(product(1,38),style='Mergulho'),dict(product(2,38),style='Dress')]
    first,_=await run(interpretation(preferences={'budget_max':6000}),rows)
    assert first.response_metadata['discovery_question']['slot']=='style'
    q=first.response_metadata['discovery_question']
    q['adaptive']['asked']=['budget','color','occasion']
    i=interpretation()
    result,calls=await run(i,rows,[turn(first)],text='Mergulho')
    assert i.preferences.style=='Mergulho' or i.preferences.style=='mergulho'
    assert result is None and i._adaptive_ready and not calls


@pytest.mark.asyncio
async def test_recommendation_asks_purpose_without_gender_assumption(adaptive):
    first,_=await run(interpretation(goal='recommend',preferences={'budget_max':6000}),[product(1,38),product(2,42)])
    assert first.response_metadata['discovery_question']['slot']=='purchase_purpose'


def test_no_preference_clears_question_dimension_and_roundtrips_schema():
    from app.llm.turn_understanding import sales_to_turn_understanding
    i=interpretation(preferences={'mechanism':'automatic'})
    apply_qualification_answer(i,'tanto faz','mechanism')
    assert i.preferences.mechanism is None
    assert 'mechanism' in i.preferences.explicit_no_preferences
    sales_to_turn_understanding(i,message_text='tanto faz')


def test_prompt_memory_does_not_restore_catalog_tastes():
    from app.memory.contact_preference_memory import memories_for_current_search
    from app.memory.memory_models import ContactMemory
    name=ContactMemory(id=99,tenant_id='test',sender_key='test',memory_key='preferred_name',memory_kind='preferred_name',value='João')
    memories=[*_catalog_memories(),name]
    assert memories_for_current_search(memories,'Orient')==[name]
    assert memories_for_current_search(memories,'o mesmo de antes')==memories


def test_final_restart_keeps_delivery_data_and_order(contextual):
    from app.models import IncomingMessage, AgentResult
    from app.verify.final_response import finalize_response
    state=CommerceConversationState(order_id='123',checkout_draft={'address':{'zip_code':'88030300'}},
        active_preferences={'budget_max':2500,'material':'safira'})
    result=AgentResult(reply_text='Vamos começar de novo.',intent='commerce',response_metadata={
        'response_source':'commerce_search_restart','commerce_state':{'history_cut_inbound_id':900}})
    _,updated=finalize_response(result,incoming=IncomingMessage(text='Reinicia a conversa'),interpretation=None,previous_state=state)
    assert updated.checkout_draft.address.zip_code=='88030300'
    assert updated.order_id=='123' and updated.history_cut_inbound_id==900
    assert updated.active_preferences=={}
