import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.models import AgentResult, IncomingMessage
from app.commerce.commerce_context import CommerceConversationState
from app.sales.adaptive_discovery import prepare_discovery
from tests.sales.test_contextual_discovery import contextual, interpretation

@pytest.fixture
def adaptive(contextual):
    contextual['adaptiveDiscoveryEnabled']=True
    return contextual

def product(id,size,color='azul',price=2000,strap='aço'):
    return dict(id=str(id),name=f'Hamilton Field {size} mm {color}',brand='Hamilton',case_size=str(size),dial_color=color,current_price=price,strap_material=strap,available=True)

async def run(i,products,history=None,text='quero um Hamilton',tool=None):
    calls=[]
    async def execute(name,args):
        calls.append((name,args))
        if tool: return await tool(name,args)
        return {'products':deepcopy(products)}
    async def reply(**kwargs):
        question=kwargs['discovery_state']['contextual_question']
        return AgentResult(reply_text=question['fallback'],intent='commerce',safety_reason='commerce_clarification',response_metadata={'discovery_question':question})
    result=await prepare_discovery(interpretation=i,state=CommerceConversationState(),message=IncomingMessage(text=text),recent_turns=history or [],execute_tool=execute,generate_reply=reply)
    return result,calls

def turn(result):
    return {'role':'assistant','content':result.reply_text,'metadata':result.response_metadata}


@pytest.mark.asyncio
async def test_bare_budget_followup_recovers_brand_from_actual_discovery_query(adaptive):
    first,_=await run(interpretation(),[product(1,38,price=2000),product(2,38,price=5000)])
    assert first.response_metadata['discovery_question']['slot']=='budget'
    i=interpretation(subject={'product_type':'relógio'}, preferences={'budget_max':6500},
                     references_previous_context=False, ready_for_retrieval=True)
    _,calls=await run(i,[product(1,38),product(2,42)],[turn(first)],text='Até R$ 6.500.')
    assert i.subject.brand=='Hamilton'
    assert all(args.get('brand')=='Hamilton' for _,args in calls)

@pytest.mark.asyncio
async def test_asks_difference_observed_in_catalog(adaptive):
    result,calls=await run(interpretation(),[product(1,38),product(2,42)])
    q=result.response_metadata['discovery_question']
    assert q['slot']=='case_size'
    assert q['catalog_options']==['38 mm','42 mm']
    assert calls[0][1]['brand']=='Hamilton'


@pytest.mark.asyncio
@pytest.mark.parametrize('attributes',[[],['qual:name:Cliente'],['qual:city:Rio']])
async def test_generic_watch_request_asks_before_any_catalog_call(adaptive,attributes):
    i=interpretation(subject={'product_type':'relógio'},preferences={'attributes':attributes},
                     needs_clarification=True,ready_for_retrieval=False,references_previous_context=False)
    result,calls=await run(i,[],text='quero um relogio')
    assert calls==[]
    assert result.safety_reason=='commerce_clarification'
    assert result.response_metadata['discovery_question']['slot']=='model_intent'
    assert not i._adaptive_ready


@pytest.mark.asyncio
async def test_empty_preliminary_brand_search_preserves_qualification(adaptive):
    i=interpretation(needs_clarification=True,ready_for_retrieval=False)
    result,calls=await run(i,[])
    assert len(calls)==1 and result.safety_reason=='commerce_clarification'
    assert not i._adaptive_ready


@pytest.mark.asyncio
async def test_generic_unknown_answer_advances_contextual_question(adaptive):
    first,_=await run(interpretation(subject={'product_type':'relógio'}),[],text='quero um relógio')
    i=interpretation(subject={'product_type':'relógio'})
    result,calls=await run(i,[],[turn(first)],text='não sei')
    assert calls==[]
    assert result.response_metadata['discovery_question']['slot']=='budget'


@pytest.mark.asyncio
async def test_gift_without_product_preferences_asks_before_catalog(adaptive):
    i=interpretation(goal='recommend',subject={'product_type':'relógio'},ready_for_retrieval=True,
                     preferences={'occasion':'presente','recipient':'presente','attributes':['não entende do assunto']})
    result,calls=await run(i,[],text='Quero dar um relógio de presente, mas não entendo nada.')
    assert not calls and result.response_metadata['discovery_question']['slot']=='model_intent'


@pytest.mark.asyncio
async def test_budget_answer_keeps_ongoing_qualification_before_recommendation(adaptive):
    first,_=await run(interpretation(subject={'product_type':'relógio'}),[],text='quero um relógio')
    second,_=await run(interpretation(subject={'product_type':'relógio'}),[],[turn(first)],text='não tenho modelo')
    second.response_metadata['discovery_question']['topic']='relógios'
    i=interpretation(goal='recommend',subject={'product_type':'relógio'},
                     preferences={'budget_max':2500,'explicit_no_preferences':['brand','color','style','material','occasion','recipient','attributes']},
                     ready_for_retrieval=True,references_previous_context=False)
    third,calls=await run(i,[],[turn(first),turn(second)],text='até 2500')
    assert calls==[] and not i._adaptive_ready
    assert third.response_metadata['discovery_question']['slot']=='occasion'
    final=interpretation(goal='recommend',subject={'product_type':'relógio'},
                         preferences={'budget_max':2500,'occasion':'dia a dia'},ready_for_retrieval=True)
    result,calls=await run(final,[],[turn(first),turn(second),turn(third)],text='dia a dia')
    assert result is None and calls==[] and final._adaptive_ready

@pytest.mark.asyncio
async def test_short_answer_reuses_pool_and_filters(adaptive):
    rows=[product(1,38),product(2,42)]
    first,_=await run(interpretation(),rows)
    i=interpretation()
    second,calls=await run(i,rows,[turn(first)],text='38 mm')
    assert second is None and i._adaptive_ready
    assert calls==[]
    assert 'case_size:38-38mm' in i.preferences.attributes


@pytest.mark.asyncio
@pytest.mark.parametrize('attributes', [[], ['somente:Citizen'], ['qual:name:Cliente','somente:Citizen']])
async def test_budget_followup_with_sparse_catalog_facets_still_asks_model(adaptive, attributes):
    rows=[{'id':'1','name':'Citizen Promaster','brand':'Citizen','price':2000,'available':True},
          {'id':'2','name':'Citizen Chandler','brand':'Citizen','price':5000,'available':True}]
    first,_=await run(interpretation(subject={'brand':'Citizen'}),rows,text='Quero um Citizen')
    assert first.response_metadata['discovery_question']['slot']=='budget'
    i=interpretation(subject={'brand':'Citizen'},preferences={'budget_max':3500,'attributes':attributes})
    second,calls=await run(i,rows,[turn(first)],text='Até R$ 3.500')
    assert second.response_metadata['discovery_question']['slot']=='model_intent'
    assert not i._adaptive_ready and not calls

@pytest.mark.asyncio
async def test_fourth_question_is_allowed_when_it_distinguishes_candidates(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    q=first.response_metadata['discovery_question']
    q['slot']='occasion';q['adaptive']['asked']=['budget','color','strap']
    result,_=await run(interpretation(),[],[turn(first)],text='não sei')
    assert result.response_metadata['discovery_question']['slot']=='case_size'

@pytest.mark.asyncio
async def test_unknown_answer_never_repeats_slot(adaptive):
    rows=[product(1,38),product(2,42)]
    first,_=await run(interpretation(),rows)
    i=interpretation()
    result,calls=await run(i,rows,[turn(first)],text='não sei')
    assert result is None and i._adaptive_ready and not calls

@pytest.mark.asyncio
async def test_new_brand_invalidates_cache(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    i=interpretation(subject={'brand':'Certina','product_type':'relógio'})
    _,calls=await run(i,[],[turn(first)],text='agora quero Certina')
    assert calls[0][1]['brand']=='Certina'

@pytest.mark.asyncio
async def test_expired_candidates_are_refreshed(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    first.response_metadata['discovery_question']['adaptive']['fetched_at']=0
    _,calls=await run(interpretation(),[product(1,38)],[turn(first)],text='38 mm')
    assert calls

@pytest.mark.asyncio
@pytest.mark.parametrize('updates',[
 {'subject':{'brand':'Hamilton','model':'Murph','reference':'H70405740'}},
 {'goal':'buy','purchase_action':'create_cart'}, {'shipping_action':'quote','shipping_zipcode':'88030300'},
 {'goal':'after_sales'}, {'image_request':True}])
async def test_protected_routes_do_not_run_preliminary_search(adaptive,updates):
    result,calls=await run(interpretation(**updates),[])
    assert result is None and calls==[]

@pytest.mark.asyncio
async def test_failed_query_does_not_claim_absence(adaptive):
    async def failed(*args):return {'error':'timeout'}
    result,_=await run(interpretation(),[],tool=failed)
    assert result.safety_reason=='adaptive_discovery_unavailable'
    assert 'não encontrei' not in result.reply_text.lower()

@pytest.mark.asyncio
async def test_preview_miss_uses_full_lookup_instead_of_claiming_no_stock(adaptive):
    i=interpretation(preferences={'budget_max':1000})
    result,calls=await run(i,[product(1,38,price=2000)])
    assert result is None and i._adaptive_ready
    assert calls[0][1]['current_price_range']=='0,1000'

@pytest.mark.asyncio
async def test_search_limit_is_not_question_limit(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    snapshot=first.response_metadata['discovery_question']['adaptive']
    snapshot.update(searches=4,fetched_at=0)
    result,calls=await run(interpretation(),[],[turn(first)],text='não sei')
    assert calls==[] and result.safety_reason=='adaptive_discovery_limit'

@pytest.mark.asyncio
async def test_direct_request_skips_preliminary_search(adaptive):
    i=interpretation()
    result,calls=await run(i,[],text='mostre as opções')
    assert result is None and i._adaptive_ready and calls==[]

@pytest.mark.asyncio
async def test_price_widening_does_not_reuse_filtered_cache(adaptive):
    first,_=await run(interpretation(preferences={'budget_max':2500}),[product(1,38),product(2,42)])
    _,calls=await run(interpretation(preferences={'budget_max':5000}),[],[turn(first)],text='até 5000')
    assert calls[0][1]['current_price_range']=='0,5000'

@pytest.mark.asyncio
async def test_numeric_size_answer_preserves_requested_size(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    i=interpretation()
    _,calls=await run(i,[],[turn(first)],text='38')
    assert 'case_size:38-38mm' in i.preferences.attributes and not calls

@pytest.mark.asyncio
async def test_no_silent_relaxation_after_full_lookup(adaptive):
    from app.catalog.retrieval.near_match import handle_hard_filter_miss
    i=interpretation(preferences={'attributes':['case_size:38-38mm']});i._adaptive_ready=True
    session=SimpleNamespace(interpretation=i,hard_filtered=[])
    result=await handle_hard_filter_miss(session)
    assert result.safety_reason=='adaptive_discovery_unconfirmed'
    assert '38' in result.reply_text
    assert not result.commercial_data

def test_explicit_indifference_removes_old_size(adaptive):
    from app.sales.adaptive_discovery import _restore_preferences
    i=interpretation(preferences={'explicit_no_preferences':['case_size']})
    _restore_preferences(i,{'preferences':{'attributes':['case_size:38-38mm']}},'case_size','tanto faz')
    assert not i.preferences.attributes

@pytest.mark.asyncio
async def test_full_handler_asks_then_searches_after_size(adaptive,monkeypatch):
    from app.sales.catalog_retrieve import retrieve_catalog_or_clarify
    import app.sales_agent as sales
    from app.llm import openai_gateway
    from app.commerce.commerce_router import _product_result
    calls=[]
    async def tool(name,args):
        calls.append(name)
        return {'products':[product(1,38),product(2,42)]}
    async def generate(**kwargs):
        return SimpleNamespace(text='Você prefere a caixa de 38 mm ou de 42 mm?')
    async def compiled(i,**kwargs):
        calls.append('compiled')
        assert 'case_size:38-38mm' in i.preferences.attributes
        return _product_result('product_search',[product(1,38)])
    async def responder(*args,**kwargs):return None
    monkeypatch.setattr(sales,'execute_tool',tool)
    monkeypatch.setattr(sales,'_execute_compiled_product_retrieval',compiled)
    monkeypatch.setattr(sales,'_sales_response_with_openai',responder)
    monkeypatch.setattr(sales,'get_settings',lambda:SimpleNamespace(openai_api_key='test',openai_model='test'))
    monkeypatch.setattr(openai_gateway,'generate_text_output',generate)
    async def handle(i,text,history):
        return await retrieve_catalog_or_clarify(message=IncomingMessage(text=text),facts={},customer_context={},interpretation=i,plan={'intent':'clarification','query':'Hamilton'},state=CommerceConversationState(),recent_turns=history,resolved_product=None)
    first=await handle(interpretation(),'quero um Hamilton',[])
    assert first.response_metadata['discovery_question']['slot']=='case_size'
    final=await handle(interpretation(answer_strategy='clarify',needs_clarification=True),'38',[turn(first)])
    assert calls==['search_products','compiled']
    assert final.safety_reason!='commerce_clarification'

@pytest.mark.asyncio
async def test_show_what_you_have_stops_qualification(adaptive):
    i=interpretation()
    result,calls=await run(i,[],text='mostra o que você tem')
    assert result is None and i._adaptive_ready and not calls

@pytest.mark.asyncio
async def test_new_thread_does_not_restore_another_threads_candidates(adaptive):
    first,_=await run(interpretation(),[product(1,38),product(2,42)])
    history=[{**turn(first),'conversation_id':'old'}]
    calls=[]
    async def tool(name,args):calls.append(args);return {'products':[]}
    async def reply(**kwargs):raise AssertionError('should not ask')
    i=interpretation()
    await prepare_discovery(interpretation=i,state=CommerceConversationState(),message=IncomingMessage(text='38 mm',conversation_id='new'),recent_turns=history,execute_tool=tool,generate_reply=reply)
    assert calls

def test_invalid_operator_rules_fall_back_without_crashing(adaptive):
    from app.sales.adaptive_discovery import configuration
    for bad in ['not json','[]','null','{"facets":null}']:
        adaptive['adaptiveDiscoveryRules']=bad
        assert configuration()=={}
