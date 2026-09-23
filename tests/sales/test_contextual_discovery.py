import json
from pathlib import Path
import pytest
from app.configuration.runtime import bind_bundle, reset_bundle
from app.models import SalesInterpretation, IncomingMessage
from app.sales.discovery import _discovery_state, _persona_qualification_question

@pytest.fixture
def contextual():
    fields=json.loads(Path('sql/seeds/operator_catalog.json').read_text(encoding='utf-8'))
    values={f['key']:f['default'] for f in fields}
    values['contextualDiscoveryEnabled']=True
    tokens=bind_bundle({'values':values,'fields':fields},None)
    yield values
    reset_bundle(tokens)

def interpretation(**updates):
    data=dict(domain='commerce',goal='discover',subject={'brand':'Hamilton','product_type':'relógio'},preferences={},references_previous_context=True,needs_clarification=False,confidence=0.99)
    data.update(updates)
    return SalesInterpretation(**data)

def marker(slot,topic='hamilton'):
    return {'role':'assistant','content':'Uma redação completamente diferente?', 'metadata':{'safety_reason':'commerce_clarification','discovery_question':{'slot':slot,'topic':topic}}}

def test_brand_only_asks_model_first(contextual):
    i=interpretation()
    s=_discovery_state(i,[],message_text='quero um Hamilton')
    assert s['persona_qualification_required']
    assert s['contextual_question']['slot']=='model_intent'
    assert _persona_qualification_question(i,s).endswith('?')


def test_no_model_does_not_mean_no_preferences_on_every_facet():
    from app.sales.contextual_discovery import grounded_no_preferences
    claimed=['color','occasion','attributes']
    assert grounded_no_preferences(claimed,{'model_intent','budget'},
        [{'role':'user','content':'Não tenho nenhum modelo em mente.'}], 'Até 2500') == set()
    assert grounded_no_preferences(claimed,set(),[], 'Tanto faz a cor') == {'color'}
    assert grounded_no_preferences(claimed,set(),[], 'Sem nenhuma preferência') == set(claimed)

def test_unknown_answer_advances_by_slot_not_wording(contextual):
    s=_discovery_state(interpretation(),[marker('model_intent'),{'role':'user','content':'não sei'}],message_text='não sei')
    assert s['contextual_question']['slot']=='budget'

def test_question_limit_opens_search(contextual):
    s=_discovery_state(interpretation(),[marker(x) for x in ('model_intent','budget','occasion')],message_text='não sei')
    assert not s['persona_qualification_required']
    assert s['force_retrieval']

@pytest.mark.parametrize('text',['mostre as opções','pode buscar','sem perguntas'])
def test_explicit_request_skips_questions(contextual,text):
    s=_discovery_state(interpretation(),[],message_text=text)
    assert not s['persona_qualification_required']
    assert s['force_retrieval']

def test_brand_budget_is_sufficient(contextual):
    s=_discovery_state(interpretation(preferences={'budget_max':2500}),[],message_text='até 2500 reais')
    assert not s['persona_qualification_required']

def test_exact_model_keeps_existing_flow(contextual):
    i=interpretation(subject={'brand':'Hamilton','model':'Khaki Field Murph','reference':'H70405740'})
    s=_discovery_state(i,[],message_text='quero o Murph azul H70405740')
    assert 'contextual_question' not in s

def test_new_brand_does_not_inherit_question_history(contextual):
    s=_discovery_state(interpretation(subject={'brand':'Certina','product_type':'relógio'}),[marker('model_intent')],message_text='quero um Certina')
    assert s['contextual_question']['slot']=='model_intent'

def test_variant_does_not_reopen_discovery(contextual):
    i=interpretation(); i._variant_refinement=True
    s=_discovery_state(i,[],message_text='aço')
    assert 'contextual_question' not in s

def test_invalid_configuration_preserves_legacy(contextual):
    from app.sales.contextual_discovery import configuration
    contextual['contextualDiscoveryRules']='bad json'
    assert configuration()=={}

@pytest.mark.asyncio
async def test_generated_question_keeps_structured_marker(contextual,monkeypatch):
    from types import SimpleNamespace
    from app.sales.responder import generate_clarification_reply
    captured={}
    async def generate(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text='Você procura algum modelo específico ou quer ajuda para escolher?')
    monkeypatch.setattr('app.sales_agent.get_settings',lambda:SimpleNamespace(openai_api_key='test',openai_model='test'))
    monkeypatch.setattr('app.llm.openai_gateway.generate_text_output',generate)
    i=interpretation(); s=_discovery_state(i,[],message_text='quero um Hamilton')
    result=await generate_clarification_reply(message=IncomingMessage(text='quero um Hamilton'),interpretation=i,discovery_state=s)
    assert result.response_metadata['discovery_question']=={'slot':'model_intent','topic':'hamilton'}
    assert result.reply_text.startswith('Você procura')
    assert 'question_to_ask' in captured['messages'][-1]['content']


@pytest.mark.parametrize('prefs,text',[
 ({'attributes':['case_size:38-38mm']},'38 mm'),
 ({'attributes':['required_strap_material:aco'],'material':'aço'},'pulseira de aço')])
def test_requested_characteristic_rejects_unconfirmed_products(contextual,prefs,text):
    from app.catalog.retrieval.hard_filter import hard_filter_products
    i=interpretation(preferences=prefs,goal='recommend')
    products=[{'id':'unknown','name':'Hamilton Automatic','brand':'Hamilton','current_price':1000,'available':True}]
    assert hard_filter_products(products,i,mode='recommendation',message_text=text)==[]

@pytest.mark.asyncio
async def test_missing_details_are_loaded_before_strap_filter(contextual):
    from types import SimpleNamespace
    from app.catalog.retrieval.preference_details import confirm_preference_details
    from app.catalog.retrieval.hard_filter import hard_filter_products
    calls=[]
    async def tool(name,args):
        calls.append((name,args))
        if name=='get_product':
            return {'id':'12','brand':'Hamilton','name':'Hamilton Field','current_price':1000,'available':True}
        return {'variants':[{'id':'v1','name':'Pulseira de aço'}]}
    i=interpretation(preferences={'attributes':['required_strap_material:aco']},goal='recommend')
    s=SimpleNamespace(interpretation=i,message_text='pulseira de aço',candidates=[{'id':'12','brand':'Hamilton','name':'Hamilton Field'}],execute_tool=tool)
    await confirm_preference_details(s)
    assert [x[0] for x in calls]==['get_product','list_product_variants']
    assert hard_filter_products(s.candidates,i,mode='recommendation')

def test_ready_overrides_stale_clarify_strategy(contextual):
    i=interpretation(answer_strategy='clarify',needs_clarification=True)
    s=_discovery_state(i,[],message_text='pode buscar')
    assert s['force_retrieval'] and not s['persona_qualification_required']

@pytest.mark.parametrize('bad',[{'maxQuestions':'3'},{'readyGroups':[['brand',{}]]},{'detailLimit':20}])
def test_invalid_rules_do_not_break_conversation(contextual,bad):
    from app.sales.contextual_discovery import configuration
    rules=json.loads(contextual['contextualDiscoveryRules']);rules.update(bad)
    contextual['contextualDiscoveryRules']=json.dumps(rules)
    assert configuration()=={}

@pytest.mark.asyncio
async def test_replay_brand_unknown_budget_reaches_catalog(contextual,monkeypatch):
    from tests.sales.test_sales_discovery_budget import _run_sales
    history=[]
    i=interpretation()
    first,calls=await _run_sales(monkeypatch,i,history,text='quero um Hamilton')
    assert calls==[]
    assert first.response_metadata['discovery_question']['slot']=='model_intent'
    history.append({'role':'assistant','content':first.reply_text,'metadata':{**first.response_metadata,'safety_reason':first.safety_reason}})
    second,calls=await _run_sales(monkeypatch,interpretation(),history,text='não sei')
    assert calls==[]
    assert second.response_metadata['discovery_question']['slot']=='budget'
    history.append({'role':'assistant','content':second.reply_text,'metadata':{**second.response_metadata,'safety_reason':second.safety_reason}})
    third,calls=await _run_sales(monkeypatch,interpretation(preferences={'budget_max':2500}),history,text='até 2500 reais')
    assert calls
    assert not third.response_metadata.get('discovery_question')

@pytest.mark.asyncio
async def test_detail_failure_keeps_original_pool(contextual):
    from types import SimpleNamespace
    from app.catalog.retrieval.preference_details import confirm_preference_details
    async def fail(*args):
        raise TimeoutError('offline')
    original=[{'id':'12','brand':'Hamilton','name':'Hamilton Field'}]
    s=SimpleNamespace(interpretation=interpretation(preferences={'attributes':['case_size:38-38mm']}),message_text='38mm',candidates=original.copy(),execute_tool=fail)
    await confirm_preference_details(s)
    assert s.candidates==original

@pytest.mark.asyncio
async def test_detail_budget_is_bounded(contextual):
    from types import SimpleNamespace
    from app.catalog.retrieval.preference_details import confirm_preference_details
    calls=[]
    async def tool(name,args):
        calls.append(args)
        return {'error':'unavailable'}
    s=SimpleNamespace(interpretation=interpretation(preferences={'attributes':['case_size:38-38mm']}),message_text='38mm',candidates=[{'id':str(n),'brand':'Hamilton','name':'Hamilton Field'} for n in range(10)],execute_tool=tool)
    await confirm_preference_details(s)
    assert len(calls)==3
