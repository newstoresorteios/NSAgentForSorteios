from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import AgentResult, IncomingMessage
from app.sales.delivery_deadline import preference_update_context, resolve_delivery_requirement
from app.sales.preference_state import merge_preferences


def update(prefs, inbound_id=10, **metadata):
    return AgentResult(reply_text='Entendido', intent='commerce', response_metadata={
        'domain': 'commerce', 'active_preferences': prefs,
        'preference_update_context': {'inbound_id': inbound_id, 'source': 'customer_interpretation',
            'observed_at': '2026-10-01T22:00:00Z', 'time_source': 'message', 'confidence': None},
        **metadata})


def test_partial_turn_retains_origin_and_correction_updates_only_changed_field():
    state = evolve_commerce_state(CommerceConversationState(), update(
        {'occasion': 'casamento', 'color': 'azul', 'budget_max': 5000}, goal='discover'))
    changed = evolve_commerce_state(state, update({'color': 'preto'}, inbound_id=11))
    assert changed.active_preferences['occasion'] == 'casamento'
    assert changed.preference_provenance['occasion']['inbound_id'] == 10
    assert changed.preference_provenance['color']['inbound_id'] == 11
    assert changed.preference_provenance['color']['confidence'] is None
    assert changed.canonical_context()['goal'] == 'discover'
    assert CommerceConversationState.from_payload(changed.model_dump()).preference_provenance == changed.preference_provenance


def test_explicit_removal_is_auditable_and_legacy_origin_is_not_fabricated():
    state = CommerceConversationState(active_preferences={'budget_max': 3000, 'occasion': 'casamento'})
    changed = evolve_commerce_state(state, update({'explicit_no_preferences': ['budget']}))
    assert 'budget_max' not in changed.active_preferences
    assert changed.preference_provenance['budget_max']['status'] == 'removed'
    assert changed.preference_provenance['occasion']['source'] == 'legacy_unknown'
    assert changed.preference_provenance['occasion']['inbound_id'] is None
    assert merge_preferences({'attributes': ['ready_to_ship']}, {'explicit_no_preferences': ['attributes']})['attributes'] == []
    assert merge_preferences({'explicit_no_preferences': ['attributes']}, {'attributes': ['ready_to_ship']})['attributes'] == ['ready_to_ship']


def test_next_week_is_a_range_and_short_reply_never_reanchors_it():
    first = evolve_commerce_state(CommerceConversationState(), update(
        {'delivery_deadline_text': 'próxima semana', 'delivery_mode': 'ready_to_ship'}))
    assert first.delivery_requirement['earliest_date'] == '2026-10-05'
    assert first.delivery_requirement['latest_date'] == '2026-10-11'
    assert first.delivery_requirement['requires_date_confirmation'] is True
    assert first.delivery_requirement['arrival_confirmed'] is False
    reply = update({'color': 'azul'}, inbound_id=11)
    reply.response_metadata['preference_update_context']['observed_at'] = '2026-10-20T12:00:00Z'
    changed = evolve_commerce_state(first, reply)
    assert changed.delivery_requirement == first.delivery_requirement


def test_relative_day_respects_brasilia_boundary_and_unknown_time():
    context = {'observed_at': '2026-10-02T01:00:00Z', 'time_source': 'message'}
    assert resolve_delivery_requirement('amanhã', context)['latest_date'] == '2026-10-02'
    assert resolve_delivery_requirement('próxima semana')['resolution'] == 'unresolved'
    assert resolve_delivery_requirement('amanhã', {**context, 'time_source': 'received'})['resolution'] == 'unresolved'
    assert resolve_delivery_requirement('31/02/2026', context)['resolution'] == 'unresolved'
    assert resolve_delivery_requirement('30 dias úteis', context)['resolution'] == 'unresolved'


def test_message_time_is_distinct_from_receipt_and_missing_timezone():
    message = IncomingMessage(provider='meta', raw={'inbound_id': 9, 'meta_event': {'timestamp': 1790899200000}})
    assert preference_update_context(message)['time_source'] == 'message'
    message.raw = {'received_at': '2026-10-01T12:00:00Z'}
    assert preference_update_context(message)['time_source'] == 'received'
    message.raw = {'timestamp': '2026-10-01T12:00:00'}
    assert preference_update_context(message)['observed_at'] is None


def test_new_browse_clears_objective_metadata_without_erasing_open_order():
    from app.sales.dialogue_phase import reset_browse_memory_keep_orders
    state = evolve_commerce_state(CommerceConversationState(order_id='123', order_status='A ENVIAR'), update(
        {'occasion': 'casamento', 'delivery_deadline_text': 'amanhã'}, goal='discover'))
    state.pending_question = 'Qual cor?'
    state.questions_asked = [{'slot': 'color'}]
    reset = reset_browse_memory_keep_orders(state)
    assert reset.order_id == '123'
    assert reset.active_goal is None and reset.pending_question is None
    assert not reset.questions_asked and reset.delivery_requirement is None
    assert 'occasion' not in reset.preference_provenance
    assert 'delivery_deadline_text' not in reset.active_preferences
    from app.memory.context_resume import merge_commerce_states
    merged = merge_commerce_states(reset.model_dump(), state.model_dump())
    assert merged['delivery_requirement'] is None
    assert 'delivery_deadline_text' not in merged['active_preferences']


def test_question_history_and_pending_question_survive_fallback_then_advance():
    question = update({}, goal='discover', discovery_question={'topic': 'relogio', 'slot': 'budget'})
    question.safety_reason = 'commerce_clarification'
    question.reply_text = 'Qual é o orçamento?'
    state = evolve_commerce_state(CommerceConversationState(), question)
    state = evolve_commerce_state(state, AgentResult(reply_text='Não consegui consultar', response_metadata={'domain': 'commerce'}))
    assert state.pending_question == 'Qual é o orçamento?'
    assert state.questions_asked[0]['slot'] == 'budget'
    state = evolve_commerce_state(state, update({'budget_max': 2000}, presented_products=True))
    assert state.pending_question is None


def test_handoff_brief_contains_pending_work_and_consent_without_sensitive_values():
    from app.ops.handoff_service import enrich_handoff_metadata, handoff_provider_payload
    state = evolve_commerce_state(CommerceConversationState(), update({'occasion': 'casamento'}, goal='find'))
    state.pending_question = 'Qual o CEP?'
    state.pending_action = 'awaiting_shipping_zipcode'
    state.checkout_draft.customer.email = 'cliente@example.com'
    message = IncomingMessage(text='Quero falar com atendente. CPF 111.444.777-35, cliente@example.com',
                              raw={'inbound_id': 20})
    result = enrich_handoff_metadata(message, AgentResult(reply_text='ok'), recent_turns=[], commerce_state=state)
    brief = handoff_provider_payload(result)['summary']
    assert brief['constraints']['occasion'] == 'casamento'
    assert brief['pending_action'] == 'awaiting_shipping_zipcode'
    assert brief['consent']['confirmed'] is True
    assert brief['known_checkout_fields'] == ['email']
    assert '111.444.777-35' not in str(brief) and 'cliente@example.com' not in str(brief)


def test_order_overdue_check_uses_message_time_in_replay():
    from app.sales.order_delivery_copy import complete_order_delivery_copy
    result = AgentResult(reply_text='status', commercial_data={'order_id': '1', 'status': 'A ENVIAR',
        'tracking': {'estimated_delivery_date': '2026-09-16'}})
    before = complete_order_delivery_copy(result, 'Qual a previsão?', reference_at='2026-09-15T15:00:00Z')
    after = complete_order_delivery_copy(result, 'Qual a previsão?', reference_at='2026-10-01T15:00:00Z')
    assert 'já passou' not in before.reply_text
    assert 'já passou' in after.reply_text
    assert after.response_metadata['order_delivery_date_check']['time_source'] == 'message'


def test_delay_complaint_explains_expired_estimate_and_offers_help():
    from app.sales.order_delivery_copy import complete_order_delivery_copy
    result = AgentResult(reply_text='status', commercial_data={'order_id': '26052', 'status': 'EM PREPARAÇÃO',
        'tracking': {'estimated_delivery_date': '2026-09-16'}})
    answer = complete_order_delivery_copy(result, 'Está atrasado', reference_at='2026-10-01T15:00:00Z')
    assert 'já passou' in answer.reply_text
    assert 'nova previsão' in answer.reply_text
    assert answer.response_metadata['handoff']['offer']
    assert not answer.response_metadata['handoff']['required']
    assert not answer.handoff_required


def test_delay_report_without_estimate_is_not_claimed_as_verified_delay():
    from app.sales.order_delivery_copy import complete_order_delivery_copy
    result = AgentResult(reply_text='status', commercial_data={'order_id': '26052', 'status': 'EM PREPARAÇÃO', 'tracking': {}})
    answer = complete_order_delivery_copy(result, 'Está atrasado', reference_at='2026-10-01T15:00:00Z')
    assert 'relatando atraso' in answer.reply_text
    assert not answer.response_metadata['order_delivery_date_check']['overdue']


def test_order_date_recheck_preserves_summary_and_clears_only_own_stale_offer():
    from app.sales.order_delivery_copy import complete_order_delivery_copy
    result = AgentResult(reply_text='status', commercial_data={'order_id': '1', 'status': 'A ENVIAR',
        'tracking': {'estimated_delivery_date': '2026-09-16'}},
        response_metadata={'handoff': {'summary': {'objective': 'after_sales'}}})
    late = complete_order_delivery_copy(result, 'Qual a previsão?', reference_at='2026-10-01T15:00:00Z')
    assert late.response_metadata['handoff']['summary'] == {'objective': 'after_sales'}
    timely = complete_order_delivery_copy(late, 'Qual a previsão?', reference_at='2026-09-15T15:00:00Z')
    assert timely.response_metadata['handoff']['offer'] is False
    assert 'já passou' not in timely.reply_text
