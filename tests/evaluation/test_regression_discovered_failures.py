from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.models import AgentResult,IncomingMessage,SalesInterpretation
from app.commerce.commerce_context import CommerceConversationState,resolve_commerce_reference


def test_current_product_binds_only_unambiguous_shortlist():
    interpretation=SalesInterpretation(domain='commerce',goal='inspect',reference_type='current_product',
        references_previous_context=True,needs_clarification=False,confidence=.99)
    state=CommerceConversationState(last_presented_products=[{'position':1,'product_id':'1','name':'Watch One'}])
    assert resolve_commerce_reference(interpretation,state)[0].product_id=='1'
    state.last_presented_products.append(type(state.last_presented_products[0])(position=2,product_id='2',name='Watch Two'))
    assert resolve_commerce_reference(interpretation,state)[0] is None


def test_short_factual_answer_preserves_product_without_repeating_full_catalog_name():
    from app.verify.final_response import finalize_response
    interpretation=SalesInterpretation(domain='commerce',goal='inspect',reference_type='current_product',
        references_previous_context=True,answer_strategy='search_catalog',needs_clarification=False,confidence=.99)
    state=CommerceConversationState(active_domain='commerce',active_product={'product_id':'1','name':'Watch One'},
        last_presented_products=[{'position':1,'product_id':'1','name':'Watch One'}])
    result=AgentResult(reply_text='Ele tem caixa de 40 mm.',intent='commerce',
        commercial_data={'products':[{'id':'1','name':'Watch One','properties':{'case_size':'40 mm'},'_revalidated':True}]},
        response_metadata={'interpretation':interpretation.model_dump()})
    result,after=finalize_response(result,incoming=IncomingMessage(text='Qual o tamanho dele?'),
                                  interpretation=interpretation,previous_state=state)
    assert result.commercial_data['products'][0]['id']=='1'
    assert after.active_product.product_id=='1'
    assert not after.forget_shortlist


@pytest.mark.asyncio
async def test_store_policy_answer_does_not_require_product_selection(monkeypatch):
    from app.sales.catalog_retrieve import retrieve_catalog_or_clarify
    import app.sales_agent as sales
    compose=AsyncMock(return_value=AgentResult(reply_text='Policy from published persona',intent='commerce'))
    monkeypatch.setattr(sales,'_sales_response_with_openai',compose)
    interpretation=SalesInterpretation(domain='commerce',goal='buy',answer_strategy='answer_directly',
        information_needed=[],needs_clarification=False,references_previous_context=False,confidence=.99)
    result=await retrieve_catalog_or_clarify(message=IncomingMessage(text='Will I pay tax after buying?'),
        facts={},customer_context={},interpretation=interpretation,plan={'intent':'clarification','query':''},
        state=CommerceConversationState(),recent_turns=[],resolved_product=None)
    assert result.reply_text=='Policy from published persona'
    assert compose.call_args.args[1]['intent']=='policy'


def test_pix_rounding_and_reviewer_evidence_use_decimal_and_published_policy(monkeypatch):
    from app.commerce.commerce_router import _pix_cash_price
    from app.verify.persona_evidence import persona_evidence
    from app.persona.persona_runtime import PersonaRuntimeConfig
    persona=PersonaRuntimeConfig(enabled=True,pix_discount_percent=15)
    monkeypatch.setattr('app.persona.persona_runtime.get_persona_runtime',lambda:persona)
    product={'id':'1','current_price':'5999.90'}
    assert _pix_cash_price(product,None)==5099.92
    evidence=persona_evidence([product],persona)
    assert evidence['derived_informational_prices'][0]['derived_pix_price']=='5099.92'
    persona.pix_discount_percent=10
    assert _pix_cash_price(product,None)==5399.91
    assert persona_evidence([product],persona)['derived_informational_prices'][0]['derived_pix_price']=='5399.91'


@pytest.mark.asyncio
async def test_persona_handoff_strategy_is_not_replaced_by_catalog_search(monkeypatch):
    from app.agents.commerce import handle_sales_message_inner
    import app.sales_agent as sales
    lookup=AsyncMock(side_effect=AssertionError('Appraisal must not search catalog'))
    monkeypatch.setattr(sales,'_handle_sales_catalog_inner',lookup)
    interpretation=SalesInterpretation(domain='commerce',goal='find',answer_strategy='handoff',
        subject={'model':'Customer owned watch'},references_previous_context=False,needs_clarification=False,confidence=.99)
    result=await handle_sales_message_inner(IncomingMessage(text='I want to sell my watch'),{}, {},
        semantic_plan=interpretation,commerce_state=CommerceConversationState())
    assert not result.handoff_required
    assert result.response_metadata['handoff']['offer']
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_shipping_without_cart_uses_live_product_leadtime_without_claiming_quote():
    from app.commerce.shipping_service import quote_shipping
    execute=AsyncMock(return_value={'id':'123','name':'Test Watch','availability':'25 dias uteis',
                                   'url':'https://example.test/product/123'})
    result=await quote_shipping(state=CommerceConversationState(active_product={'product_id':'123'}),
                                zipcode='60000000',execute=execute)
    execute.assert_awaited_once_with('get_product',{'product_id':'123'})
    assert '25 dias uteis' in result.reply_text
    assert 'https://example.test/product/123' in result.reply_text
    assert result.commercial_data['shipping']['quote_performed'] is False


def test_valid_catalog_fallback_survives_rejected_draft_but_invalid_fallback_stays_blocked():
    from app.verify.factual_validator import apply_factual_validation
    from app.llm.agent_contracts import build_agent_decision
    for fallback,valid in [('O produto custa R$ 100,00.',True),('O produto custa R$ 900,00.',False)]:
        result=AgentResult(reply_text='O produto custa R$ 999,00.',intent='commerce',
            commercial_data={'products':[{'id':'1','name':'Watch','current_price':'100.00'}]},
            response_metadata={'factual_fallback_text':fallback,'response_source':'openai','domain':'commerce'})
        decision=build_agent_decision(IncomingMessage(text='Qual o preco?'),result,openai_call_count=1)
        result=apply_factual_validation(result,decision=decision,mode='enforce')
        assert (result.safety_reason is None) == valid
        assert bool(result.response_metadata.get('factual_validation_repaired')) == valid


def test_critique_inspection_keeps_original_sheet_and_product_id_never_becomes_order_id():
    from app.verify.response_critique import apply_search_products_to_result, _merge_payment_and_order_facts
    source=AgentResult(reply_text='answer',intent='commerce',commercial_data={'products':[{'id':'1','crystal':'Hardlex'}]},
                       response_metadata={'interpretation':{'goal':'inspect'}})
    replacement=apply_search_products_to_result(result=source,api_facts={'search_products':{'products':[{'id':'2','crystal':'Safira'}]}})
    assert replacement.commercial_data['products'][0]['id']=='1'
    facts=_merge_payment_and_order_facts({}, {'get_product':{'id':'1','current_price':100}},None)
    assert facts['products'][0]['current_price']==100
    assert 'order_id' not in facts
    order=_merge_payment_and_order_facts(facts, {'get_order_complete':{'id':'ORDER'}},None)
    assert order['order_id']=='ORDER'


def test_catalog_female_label_is_not_treated_as_customer_name():
    from app.sales.answer_council import check_pedido,TurnContract
    product={'id':'1','name':'Relogio Feminino','price':100}
    result=AgentResult(reply_text='1. Relogio Feminino',intent='commerce',commercial_data={'products':[product]},
                      response_metadata={'interpretation':{'preferences':{'recipient':'feminino'}}})
    assert 'commerce_phrase_used_as_name' not in check_pedido(result,TurnContract(asked_text='feminino')).issues
    result.reply_text='Feminino, como posso ajudar?'
    assert 'commerce_phrase_used_as_name' in check_pedido(result,TurnContract(asked_text='feminino')).issues


def test_operator_question_regex_works_without_question_mark_and_does_not_authorize_purchase():
    from app.sales.purchase_selection import is_product_information_question,parse_list_position_selection
    assert is_product_information_question('o primeiro e automatico')
    assert is_product_information_question('quanto custa o primeiro')
    assert parse_list_position_selection('o primeiro e automatico') is None
    assert not is_product_information_question('pode fechar o primeiro')


def test_sensitive_card_and_defect_routes_use_published_copy():
    from app.sales.conversation_preflight import preflight_reply,normalize_identity
    assert 'CVV' in preflight_reply('Posso mandar meu CVV aqui?').reply_text
    offer = preflight_reply('Recebi meu relogio com defeito, quero devolver')
    assert not offer.handoff_required and offer.response_metadata['handoff']['offer']
    assert preflight_reply('Quero comprar um relogio') is None
    interpretation=SalesInterpretation(domain='commerce',goal='find',references_previous_context=False,
        needs_clarification=False,confidence=.9,subject={'brand':'Oriente','model':'Kamazu'})
    assert normalize_identity(interpretation).subject.brand=='Orient'
    assert normalize_identity(interpretation).subject.model=='Kamasu'


@pytest.mark.asyncio
async def test_comparison_fetches_separate_references_and_preserves_both_facts(monkeypatch):
    from app.sales.product_comparison import try_product_comparison
    import app.sales_agent as sales
    calls=[]
    async def tool(name,args):
        calls.append((name,args))
        if name=='search_products':return {'products':[{'id':args['reference'],'reference':args['reference']}]}
        return {'id':args['product_id'],'reference':args['product_id'],'name':'Product '+args['product_id'],'price':100}
    compose=AsyncMock(return_value=None)
    monkeypatch.setattr(sales,'execute_tool',tool)
    monkeypatch.setattr(sales,'_sales_response_with_openai',compose)
    interpretation=SalesInterpretation(domain='commerce',goal='compare',references_previous_context=False,
                                        needs_clarification=False,confidence=.9)
    result=await try_product_comparison(IncomingMessage(text='Compare AA-0001 e BB200'),interpretation,
                                       CommerceConversationState(),[])
    assert [args['reference'] for name,args in calls if name=='search_products']==['AA-0001','BB200']
    assert {p['id'] for p in result.commercial_data['products']}=={'AA-0001','BB200'}
    assert result.response_metadata['interpretation']['goal']=='compare'


def test_final_comparison_does_not_treat_question_as_filter():
    from app.verify.final_response import finalize_response
    interpretation=SalesInterpretation(domain='commerce',goal='compare',references_previous_context=True,
        needs_clarification=False,confidence=.99)
    products=[{'id':'1','name':'Watch AAA001','reference':'AAA001','price':100,'description':'Cristal Safira'},
              {'id':'2','name':'Watch BBB002','reference':'BBB002','price':100,'description':'Cristal Hardlex'}]
    result=AgentResult(reply_text='AAA001 tem safira; BBB002 usa Hardlex.',intent='commerce',
        commercial_data={'products':products},response_metadata={'interpretation':interpretation.model_dump()})
    final,_=finalize_response(result,incoming=IncomingMessage(text='Qual dos dois tem safira?'),
        interpretation=interpretation,previous_state=CommerceConversationState())
    assert 'Hardlex' in final.reply_text
    assert {p['id'] for p in final.commercial_data['products']}=={'1','2'}
    assert 'final_technical_requirements_failed' not in final.response_metadata['final_response_validation']['rejected_issues']


def test_identity_introduction_does_not_erase_a_concrete_answer():
    from app.identity.greeting_policy import is_generic_greeting_reply
    assert not is_generic_greeting_reply('Sou o Crono, assistente virtual da New Store. Esse PRX tem caixa de 40 mm.')
    assert is_generic_greeting_reply('Sou o Crono.')


@pytest.mark.parametrize("availability", ["available", "unavailable", "unknown"])
def test_unpriced_technical_match_is_not_an_offer_or_purchase_target(availability):
    from app.catalog.retrieval.technical import technical_miss
    from app.verify.final_response import finalize_response
    interpretation=SalesInterpretation(domain='commerce',goal='find',references_previous_context=False,
        needs_clarification=False,confidence=.99,preferences={'mechanism':'automatic','crystal':'sapphire','budget_max':2500})
    result=technical_miss(interpretation,unknown=True,evidence=[{'stage':'live_detail','status':'matched',
        'product_id':'1','product_name':'Orient Mako AAA001','reference':'AAA001','brand':'Orient',
        'product_url':'https://example.test/aaa001','commercial':{'price_status':'missing','upon_request':True,'availability':availability}}])
    final,state=finalize_response(result,incoming=IncomingMessage(text='Automático com safira até 2500'),
        interpretation=interpretation,previous_state=CommerceConversationState())
    assert final.commercial_data['products']==[]
    assert state.active_product is None
    assert not state.purchase_target
    assert 'Orient Mako' not in final.reply_text
    assert state.last_presented_products==[]


def test_explicit_brand_denial_and_color_correction_resume_search_without_buying():
    from app.sales.contextual_questions import normalize_followup
    state=CommerceConversationState(active_preferences={'subject_brand':'Tag Heuer','budget_max':2500},
        active_product={'product_id':'1','brand':'Tag Heuer'})
    interpretation=SalesInterpretation(domain='commerce',goal='find',references_previous_context=True,
        needs_clarification=False,confidence=.9,subject={'brand':'Tag Heuer'},preferences={'budget_max':2500},
        answer_strategy='acknowledge',reference_type='current_product')
    clean,after=normalize_followup('Que disse que era tag',interpretation,state)
    assert clean.subject.brand is None and 'brand' in clean.preferences.explicit_no_preferences
    assert after.active_product is None and clean.preferences.budget_max==2500
    assert clean.answer_strategy=='search_catalog' and clean.purchase_action is None
    assert state.active_product is not None
    clean,_=normalize_followup('Corrigindo: preto, não azul',interpretation,state)
    assert clean.reference_type is None and clean.answer_strategy=='search_catalog'


@pytest.mark.asyncio
async def test_known_product_question_fetches_facts_before_discovery_hold(monkeypatch):
    from app.sales.contextual_questions import try_contextual_question
    import app.sales_agent as sales
    target={'product_id':'1','name':'Kamasu AAA001','brand':'Orient','reference':'AAA001'}
    state=CommerceConversationState(active_product=target)
    interpretation=SalesInterpretation(domain='commerce',goal='inspect',references_previous_context=True,
        needs_clarification=False,confidence=.9,reference_type='current_product')
    lookup=AsyncMock(return_value=AgentResult(reply_text='Consulta indisponível',intent='commerce',
                                             safety_reason='tray_adapter_unavailable'))
    monkeypatch.setattr(sales,'_execute_contextual_product_lookup',lookup)
    result=await try_contextual_question(IncomingMessage(text='Qual o preço desse Kamasu?'),interpretation,state,[])
    assert lookup.await_count==1
    assert result.safety_reason=='tray_adapter_unavailable'
    assert result.response_metadata['interpretation']['purchase_action'] is None


@pytest.mark.asyncio
async def test_rate_limit_does_not_retry_same_provider_via_chat():
    from app.llm.openai_gateway import FallbackOpenAIGateway
    from app.llm.openai_errors import OpenAIRateLimitGatewayError
    primary=SimpleNamespace(parse_structured=AsyncMock(side_effect=OpenAIRateLimitGatewayError(code='openai_quota_exhausted')))
    fallback=SimpleNamespace(parse_structured=AsyncMock())
    gateway=FallbackOpenAIGateway(primary=primary,fallback=fallback)
    with pytest.raises(OpenAIRateLimitGatewayError):
        await gateway.parse_structured(model='model',text_format=SalesInterpretation,messages=[])
    fallback.parse_structured.assert_not_called()
