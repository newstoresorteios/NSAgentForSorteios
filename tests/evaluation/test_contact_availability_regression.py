from unittest.mock import AsyncMock
import pytest
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.commerce.commerce_context import CommerceConversationState


def interpretation(**values):
    values.setdefault('references_previous_context', False)
    return SalesInterpretation(domain='commerce', goal='find', confidence=.99,
        needs_clarification=False, **values)


def test_variant_fragment_refines_search_without_authorizing_purchase():
    from app.sales.contextual_questions import normalize_followup
    request = SalesInterpretation(domain='commerce', goal='inspect', confidence=.99, needs_clarification=False,
        references_previous_context=True, reference_type='last_presented_product', reference_position=1,
        subject={'brand':'Baltic','model':'MK2'}, preferences={'color':'cinza','attributes':['case_size:37-37mm']})
    state = CommerceConversationState(active_product={'product_id':'OLD','name':'Baltic Azul'},
        last_presented_products=[{'product_id':'OLD','position':1,'name':'Baltic Azul'}])
    updated, memory = normalize_followup('O cinza, de 37 mm', request, state)
    assert updated.goal == 'find' and updated.answer_strategy == 'search_catalog'
    assert updated.subject.model == 'MK2' and updated.preferences.color == 'cinza'
    assert updated.reference_position is None and updated.purchase_action is None
    assert not memory.last_presented_products and memory.active_product is None
    assert state.active_product is not None


def test_short_material_answer_keeps_baltic_model_and_refines_variant():
    from app.sales.contextual_questions import normalize_followup

    request = SalesInterpretation(
        domain='commerce', goal='buy', confidence=.99, needs_clarification=False,
        references_previous_context=True, reference_type='last_presented_product',
        subject={'brand':'Baltic','model':'Aquascaphe MK2'},
        preferences={'material':'aço inoxidável','attributes':['case_size:37-37mm']},
        answer_strategy='acknowledge',
    )
    state = CommerceConversationState(last_presented_products=[{
        'product_id':'14738', 'position':1,
        'name':'Relógio Baltic Aquascaphe MK2 Automático Cinza 37mm',
        'brand':'Baltic',
    }])

    updated, memory = normalize_followup('Aço', request, state)

    assert updated.goal == 'find'
    assert updated.answer_strategy == 'search_catalog'
    assert updated.subject.model == 'Aquascaphe MK2'
    assert updated.purchase_action is None
    assert memory.last_presented_products[0].product_id == '14738'


def test_same_brand_strap_refinement_keeps_model_across_conversation_ids():
    from app.sales.answer_council import build_turn_contract, apply_turn_contract_for_search

    state = CommerceConversationState(
        last_presented_products=[{
            'product_id':'14738', 'position':1,
            'name':'Relógio Baltic Aquascaphe MK2 Automático Cinza 37mm',
            'brand':'Baltic',
        }],
        active_preferences={'subject_brand':'Baltic','subject_model':'Aquascaphe MK2'},
    )
    request = interpretation(
        subject={'brand':'Baltic','product_type':'relógio'},
        preferences={'material':'aço'},
    )
    contract = build_turn_contract(
        message_text='Quero o Baltic com pulseira de aço',
        interpretation=request,
        commerce_state=state,
    )
    corrected = apply_turn_contract_for_search(
        request,
        message_text='Quero o Baltic com pulseira de aço',
        commerce_state=state,
    )

    assert contract.model and 'mk2' in contract.model.casefold()
    assert contract.material == 'aço'
    assert corrected.subject.model and 'mk2' in corrected.subject.model.casefold()
    assert 'required_strap_material:aço' in corrected.preferences.attributes


def test_inspecting_list_position_is_not_purchase_routing():
    from app.sales.intent_router import route_sales_intent
    request = SalesInterpretation(domain='commerce',goal='inspect',confidence=.99, needs_clarification=False,
        references_previous_context=True,
        reference_position=1, answer_strategy='answer_directly')
    route = route_sales_intent(interpretation=request,plan={'intent':'product_search'},
        message_text='Qual o tamanho desse?',commerce_state=CommerceConversationState())
    assert route.route_kind == 'inspect'


def test_buying_intent_without_a_selected_product_searches_first():
    from app.sales.contextual_questions import normalize_followup
    request=SalesInterpretation(domain='commerce',goal='buy',confidence=.99,needs_clarification=False,
        references_previous_context=False,answer_strategy='search_catalog',subject={'brand':'Certina','product_type':'relógio'})
    updated,_=normalize_followup('Gostaria de comprar um Certina a pronta entrega',request,CommerceConversationState())
    assert updated.goal=='find' and updated.purchase_action is None
    request.purchase_action='create_cart'
    explicit,_=normalize_followup('Quero comprar',request,CommerceConversationState())
    assert explicit.goal=='buy' and explicit.purchase_action=='create_cart'


def test_interest_alone_does_not_authorize_cart():
    from app.sales.contextual_questions import normalize_followup
    request=SalesInterpretation(domain='commerce',goal='buy',confidence=.99,needs_clarification=False,
        references_previous_context=True,purchase_action='create_cart',confirmation='confirm')
    updated,_=normalize_followup('Me interessei nesse',request,CommerceConversationState())
    assert updated.goal=='inspect' and updated.purchase_action is None and updated.confirmation=='none'


def test_alternative_search_releases_unavailable_model():
    from app.sales.contextual_questions import normalize_followup
    request=interpretation(subject={'brand':'Orient','model':'Open Heart','reference':'RA-AG0029N10B'},
        references_previous_context=True,preferences={'mechanism':'automatic','crystal':'sapphire','budget_max':2500})
    request.goal='recommend'
    state=CommerceConversationState(active_product={'product_id':'5','name':'Open Heart'},
        active_preferences={'subject_model':'Open Heart'})
    updated,memory=normalize_followup('Então me mostra outro automático com safira até 2500',request,state)
    assert updated.subject.model is None and updated.subject.reference is None
    assert updated.preferences.budget_max==2500 and memory.active_product is None
    assert 'subject_model' not in memory.active_preferences


def test_new_model_of_same_brand_does_not_select_old_shortlist():
    from app.sales.purchase_selection import repair_presented_purchase_selection
    request=interpretation(subject={'brand':'Baltic','model':'MK2'})
    request.goal='buy'
    state=CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Baltic MR01','brand':'Baltic'},
        {'position':2,'product_id':'2','name':'Baltic Hermetique','brand':'Baltic'}])
    updated=repair_presented_purchase_selection(request,message_text='Quero o Baltic MK2',state=state)
    assert updated.goal=='find' and updated.purchase_action is None
    assert not updated.needs_clarification and updated.subject.model=='MK2'


def test_evaluator_preserves_handoff_offer_for_next_turn_consent():
    from app.evaluation.judge import compact_metadata
    metadata={'handoff':{'offer':True,'required':False},'internal_noise':'unused'}
    assert compact_metadata(metadata)['handoff']==metadata['handoff']


def test_shared_model_family_remains_locked_across_budget_answer():
    from app.sales.turn_contract import locked_identity_from_state
    state=CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Longines Heritage Preto','brand':'Longines'},
        {'position':2,'product_id':'2','name':'Longines Heritage Prata','brand':'Longines'}])
    brand,model=locked_identity_from_state(state)
    assert brand=='Longines' and model.casefold()=='heritage'


def test_readonly_media_followup_overrides_inferred_purchase():
    from app.sales.contextual_questions import normalize_followup
    request=interpretation(references_previous_context=True)
    request.goal='buy';request.purchase_action='create_cart'
    updated,_=normalize_followup('Não, quero esse que me sugeriu',request,CommerceConversationState(),
        [{'role':'user','content':'Tem foto?'},{'role':'assistant','content':'Pode enviar uma foto?'}])
    assert updated.image_request and updated.purchase_action is None and updated.goal=='inspect'


def test_color_refinement_does_not_buy_another_variant():
    from app.sales.purchase_selection import repair_presented_purchase_selection
    request=interpretation(subject={'brand':'Baltic','model':'mk2'},preferences={'color':'cinza'})
    request.goal='buy'
    state=CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Baltic MK2 Prata','brand':'Baltic'}])
    updated=repair_presented_purchase_selection(request,message_text='quero o mk2 cinza 37mm',state=state)
    assert updated.goal=='find' and updated.purchase_action is None


@pytest.mark.asyncio
async def test_name_acknowledgement_survives_council_with_model_family(monkeypatch):
    from app.sales.contextual_questions import try_name_answer
    from app.sales.answer_council import apply_answer_council_with_retry
    import app.sales.product_lookup as lookup
    remote=AsyncMock(side_effect=AssertionError('A name answer does not trigger retrieval'))
    monkeypatch.setattr(lookup,'execute_compiled_product_retrieval',remote)
    request=interpretation(subject={'brand':'Longines','model':'Heritage'},active_topic='Longines Heritage preto')
    request.answer_strategy='acknowledge'
    state=CommerceConversationState()
    result=try_name_answer(IncomingMessage(text='Sou o João'),request,state)
    final,_,_=await apply_answer_council_with_retry(result,incoming=IncomingMessage(text='Sou o João'),
        interpretation=request,commerce_state=state)
    assert 'João' in final.reply_text and 'Heritage' in final.reply_text
    remote.assert_not_called()


def test_price_verifier_receives_on_request_and_availability_evidence():
    from app.verify.double_check import _phase1_packet
    item={'id':'1','name':'Watch','price':0,'upon_request':'1','available':'1',
          'availability':'Disponível em 30 dias úteis','url':'https://example.test/item'}
    packet=_phase1_packet(incoming=IncomingMessage(text='Quanto custa?'),result=AgentResult(reply_text='Preço sob consulta',
        intent='commerce',commercial_data={'products':[item]}),commerce_state=None,signals=[])
    assert packet['products'][0]['upon_request']=='1'
    assert packet['products'][0]['availability']==item['availability']
    assert packet['products'][0]['url']==item['url']


def test_name_answer_keeps_pending_shopping_topic():
    from app.sales.contextual_questions import try_name_answer
    request=interpretation(active_topic='Longines Heritage preto')
    request._slot_answer_hold=True
    result=try_name_answer(IncomingMessage(text='Sou o João'),request,CommerceConversationState())
    assert 'João' in result.reply_text and 'Longines Heritage preto' in result.reply_text
    assert 'marca' not in result.reply_text and 'modelo' not in result.reply_text
    assert not result.handoff_required


@pytest.mark.asyncio
async def test_semantic_reference_recovers_product_for_interest_fragment(monkeypatch):
    from app.sales.contextual_questions import recover_mentioned_product
    import app.sales_agent as sales
    lookup=AsyncMock(return_value={'products':[{'id':'1','reference':'MK2TEST37','name':'Baltic MK2'}]})
    monkeypatch.setattr(sales,'execute_tool',lookup)
    request=interpretation(references_previous_context=True)
    result=await recover_mentioned_product(IncomingMessage(text='Me interessei nesse'),CommerceConversationState(),
        [{'role':'assistant','content':'O Baltic MK2TEST37 foi consultado.'}],request)
    assert result.active_product.product_id=='1'


@pytest.mark.asyncio
async def test_link_wording_repair_keeps_verified_url_without_catalog_retrieval(monkeypatch):
    from app.sales.answer_council import apply_answer_council_with_retry
    import app.sales.product_lookup as lookup
    remote = AsyncMock(side_effect=AssertionError('A wording repair must not change the product'))
    monkeypatch.setattr(lookup,'execute_compiled_product_retrieval',remote)
    request = SalesInterpretation(domain='commerce',goal='inspect',confidence=.99, needs_clarification=False,
        reference_type='current_product',references_previous_context=True, subject={'brand':'Orient','reference':'TEST001'})
    state = CommerceConversationState(active_product={'product_id':'1','name':'Watch','brand':'Orient'})
    result = AgentResult(reply_text='Link: https://example.test/item. Como posso te chamar?',intent='commerce',
        commercial_data={'product_link':{'product_id':'1','product_url':'https://example.test/item',
                                         'product_url_dead':False}})
    corrected, _, _ = await apply_answer_council_with_retry(result,
        incoming=IncomingMessage(text='Me dá o link para eu ver'),interpretation=request,commerce_state=state)
    assert 'https://example.test/item' in corrected.reply_text
    assert 'chamar' not in corrected.reply_text
    assert not corrected.commercial_data.get('products')
    remote.assert_not_called()


def test_explicit_gender_and_ready_requirement_filter_catalog_candidates():
    from app.catalog.retrieval.hard_filter import hard_filter_products
    rows = [
        {'id':'1', 'name':'Relógio Feminino', 'order_days_availability':30},
        {'id':'2', 'name':'Relógio Feminino', 'related_categories':['403']},
        {'id':'3', 'name':'Relógio Masculino', 'related_categories':['403']},
    ]
    request = interpretation(preferences={'recipient':'feminino','attributes':['ready_to_ship']})
    assert [p['id'] for p in hard_filter_products(rows, request, mode='recommendation')] == ['2']


def test_ready_requirement_survives_model_refinement_and_can_be_released():
    from app.sales.contextual_questions import normalize_ready_requirement
    request = interpretation(subject={'brand':'Baltic','model':'MK2'}, references_previous_context=True)
    history = [{'role':'user','content':'Quero um Baltic a pronta entrega'}]
    updated = normalize_ready_requirement('Quero o MK2', request, CommerceConversationState(), history)
    assert 'ready_to_ship' in updated.preferences.attributes
    released = normalize_ready_requirement('Pode ser sob encomenda', updated, CommerceConversationState(), history)
    assert 'ready_to_ship' not in released.preferences.attributes


def test_named_model_preserves_delivery_constraint_without_deictic_marker():
    from app.sales.contextual_questions import normalize_ready_requirement
    request = interpretation(subject={'brand':'Baltic','model':'MK2'}, references_previous_context=False)
    history = [{'role':'user','content':'Quero um Baltic a pronta entrega'}]
    updated = normalize_ready_requirement('Quero o Baltic MK2', request, CommerceConversationState(), history)
    assert 'ready_to_ship' in updated.preferences.attributes
    changed = request.model_copy(update={'domain_change_explicit':True})
    assert 'ready_to_ship' not in normalize_ready_requirement('Novo assunto', changed, CommerceConversationState(), history).preferences.attributes


def test_mixed_shortlist_does_not_invent_shared_model_family():
    from app.sales.turn_contract import locked_identity_from_state, inbound_from_memory
    state = CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Seiko 5 SRPD55','brand':'Seiko'},
        {'position':2,'product_id':'2','name':'Seiko Speedtimer SPB515','brand':'Seiko'},
    ])
    assert locked_identity_from_state(state) == ('Seiko', None)
    exact = interpretation(subject={'brand':'Seiko','reference':'SRPD55'})
    assert inbound_from_memory(exact, state).model is None


def test_current_model_removes_borrowed_family():
    from app.catalog.specs.preference_normalize import repair_specific_model_tokens
    request = interpretation(subject={'brand':'Orient','model':'Open Heart PRX'})
    repair_specific_model_tokens(request.subject, request.preferences,
        message_text='Agora quero o Orient Open Heart preto', context_text='Quero o Tissot PRX')
    assert request.subject.model == 'Open Heart'


@pytest.mark.asyncio
async def test_history_reference_is_revalidated_before_recovering_product(monkeypatch):
    from app.sales.contextual_questions import recover_mentioned_product
    import app.sales_agent as sales
    lookup = AsyncMock(return_value={'products':[{'id':'1','reference':'RA-AA0820R19B','name':'Orient Mako'}]})
    monkeypatch.setattr(sales,'execute_tool',lookup)
    history = [{'role':'assistant','content':'Mencionei o Orient Mako RA-AA0820R19B, caixa 42mm, mas sem preço confirmado.'}]
    state = await recover_mentioned_product(IncomingMessage(text='tem foto?'), CommerceConversationState(), history)
    assert state.active_product.product_id == '1'
    assert lookup.call_args.args[1]['reference'] == 'RA-AA0820R19B'
    lookup.return_value = {'products':[{'id':'2','reference':'OTHER123','name':'Other'}]}
    state = await recover_mentioned_product(IncomingMessage(text='me dá o link'), CommerceConversationState(), history)
    assert state.active_product is None


@pytest.mark.asyncio
async def test_history_with_two_products_does_not_choose_arbitrarily(monkeypatch):
    from app.sales.contextual_questions import recover_mentioned_product
    import app.sales_agent as sales
    lookup = AsyncMock()
    monkeypatch.setattr(sales,'execute_tool',lookup)
    state = await recover_mentioned_product(IncomingMessage(text='tem foto?'), CommerceConversationState(),
        [{'role':'assistant','content':'Seiko SRPD55 ou Orient RA-AA0820R19B'}])
    assert state.active_product is None
    lookup.assert_not_called()


def test_empty_supplemental_search_keeps_verified_matching_product():
    from app.verify.response_critique import apply_search_products_to_result
    request = interpretation(preferences={'recipient':'feminino','budget_max':3000})
    source = AgentResult(reply_text='answer', intent='commerce', commercial_data={'products':[
        {'id':'1','name':'Feminino Lovely','current_price':2000,'_revalidated':True},
        {'id':'2','name':'Masculino','current_price':1000,'_revalidated':True}]},
        response_metadata={'interpretation':request.model_dump()})
    updated = apply_search_products_to_result(result=source, api_facts={'search_products':{'products':[]}})
    assert [p['id'] for p in updated.commercial_data['products']] == ['1']


def test_evaluator_compression_preserves_conflicting_prices_and_photo_evidence():
    from app.evaluation.regression_judge import compact_catalog_evidence
    product = {'id':'1','name':'Watch','price':100,'primary_image_url':'https://example.test/watch.jpg'}
    payload = compact_catalog_evidence({'products':[product]},[
        {'tool':'search_products','result':{'products':[product]}},
        {'tool':'get_product','result':{**product,'price':120}},
    ])
    assert len(payload['catalog_evidence']) == 2
    assert payload['facts']['products'][0] == payload['tools'][0]['result']['products'][0]
    assert {p['price'] for p in payload['catalog_evidence'].values()} == {100,120}
    assert all(p['primary_image_url'] for p in payload['catalog_evidence'].values())


@pytest.mark.asyncio
async def test_delivery_question_reuses_list_without_entering_purchase_selection(monkeypatch):
    from app.sales.contextual_questions import try_availability_question
    import app.sales_agent as sales
    lookup = AsyncMock(side_effect=[
        {'id':'1','name':'Certina One','availability':'Disponível em 30 dias úteis'},
        {'id':'2','name':'Certina Two','availability':'Pronta entrega'},
    ])
    compose = AsyncMock(return_value=None)
    monkeypatch.setattr(sales,'execute_tool',lookup)
    monkeypatch.setattr(sales,'_sales_response_with_openai',compose)
    state = CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Certina One'},
        {'position':2,'product_id':'2','name':'Certina Two'},
    ])
    request = interpretation(subject={'model':'Qual é o prazo de entrega?'},
        shipping_action='quote')
    result = await try_availability_question(IncomingMessage(text='Qual é o prazo de entrega?'), request, state, [])
    assert '30 dias úteis' in result.reply_text and 'Pronta entrega' in result.reply_text
    assert [call.args[0] for call in lookup.call_args_list] == ['get_product','get_product']
    assert result.response_metadata['interpretation']['subject']['model'] is None
    assert result.response_metadata['interpretation']['shipping_action'] is None
    assert not result.handoff_required


@pytest.mark.asyncio
async def test_purchase_availability_keeps_explicit_disabled_sale_despite_positive_stock(monkeypatch):
    from app.sales.contextual_questions import try_availability_question
    import app.sales_agent as sales
    lookup=AsyncMock(return_value={'id':'1','name':'Orient Open Heart','stock':38,'available':'0',
        'available_in_store':'0','available_for_purchase':'0','availability':'Disponível em 30 dias úteis'})
    compose=AsyncMock(side_effect=AssertionError('Do not rewrite an explicit disabled-sale answer'))
    monkeypatch.setattr(sales,'execute_tool',lookup)
    monkeypatch.setattr(sales,'_sales_response_with_openai',compose)
    state=CommerceConversationState(active_product={'product_id':'1','name':'Orient Open Heart','brand':'Orient'})
    request=interpretation(subject={'brand':'Orient','model':'Open Heart'})
    request.goal='buy'
    result=await try_availability_question(IncomingMessage(text='Esse Orient Open Heart está disponível para comprar?'),request,state,[])
    assert result is not None and 'indisponível para compra' in result.reply_text
    assert 'Disponível em 30 dias úteis' in result.reply_text and result.commercial_data['availability_state']=='unavailable'
    compose.assert_not_called()


def test_final_validation_uses_corrected_color_context_not_old_recommendation_brand():
    from app.verify.final_response import finalize_response
    request = interpretation(preferences={'color':'preto'}, references_previous_context=True)
    state = CommerceConversationState(last_presented_products=[
        {'position':1,'product_id':'1','name':'Citizen Azul','brand':'Citizen'}])
    result = AgentResult(reply_text='Orient Preto',intent='commerce', commercial_data={'products':[
        {'id':'2','name':'Orient Preto','brand':'Orient','color':'preto','_revalidated':True}]},
        response_metadata={'interpretation':request.model_dump()})
    final, _ = finalize_response(result,incoming=IncomingMessage(text='Corrigindo: preto, não azul'),
        interpretation=request,previous_state=state)
    assert final.reply_text == 'Orient Preto'
    assert 'fact_brand_mismatch' not in final.response_metadata['final_response_validation']['rejected_issues']
