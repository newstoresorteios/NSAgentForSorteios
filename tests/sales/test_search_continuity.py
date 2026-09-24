from app.commerce.commerce_context import CommerceConversationState
from app.sales.search_continuity import reconcile_search_turn
from app.catalog.specs.requirements import normalize_requirements
from app.sales.qualification_slots import merge_persisted_qualification_slots
from tests.sales.test_contextual_discovery import interpretation


def test_refinement_does_not_invent_brand_indifference():
    state = CommerceConversationState(active_preferences={
        'subject_brand': 'Orient', 'budget_max': 3500, 'style': 'social',
        'material': 'aço', 'mechanism': 'automatic',
    })
    i = interpretation(subject={}, preferences={'mechanism': 'quartz',
        'explicit_no_preferences': ['brand', 'color', 'attributes']})
    reconcile_search_turn(i, state, 'Prefiro quartzo, não automático. Mantenha o restante.')
    assert i.subject.brand == 'Orient'
    assert i.preferences.budget_max == 3500
    assert i.preferences.material == 'aço'
    assert i.preferences.mechanism == 'quartz'
    assert 'brand' not in i.preferences.explicit_no_preferences


def test_released_movement_is_not_resurrected_by_technical_normalization():
    state = CommerceConversationState(active_preferences={'subject_brand': 'Orient',
        'mechanism': 'quartz', 'material': 'aço', 'budget_max': 3500})
    i = interpretation(subject={}, preferences={'mechanism': 'quartz',
        'material': 'aço', 'attributes': ['quartzo', 'required_strap_material:aço']})
    reconcile_search_turn(i, state, 'Qualquer movimento e qualquer pulseira. Mostre as opções.')
    normalize_requirements(i)
    assert i.preferences.mechanism is None
    assert i.preferences.material is None
    assert i.preferences.attributes == []
    assert i.subject.brand == 'Orient'
    assert i.preferences.budget_max == 3500


def test_pix_basis_survives_same_search():
    state = CommerceConversationState(active_preferences={'subject_brand': 'Citizen',
        'budget_max': 3000, 'budget_payment_basis': 'pix'})
    i = interpretation(subject={'brand': 'Citizen', 'reference': 'NY0120-01EE'})
    reconcile_search_turn(i, state, 'Confirme cada característica desse modelo.')
    assert i.payment_method_preference == 'pix'


def test_relaxed_movement_stays_relaxed_on_later_detail_question():
    state = CommerceConversationState(active_preferences={
        'subject_brand': 'Orient', 'explicit_no_preferences': ['mechanism']})
    i = interpretation(preferences={'mechanism': 'quartz'})
    reconcile_search_turn(i, state, 'E quais opções você encontrou?')
    normalize_requirements(i)
    assert i.preferences.mechanism is None
    reconcile_search_turn(i, state, 'Agora quero automático.')
    normalize_requirements(i, 'Agora quero automático.')
    assert i.preferences.mechanism == 'automatic'


def test_historical_terminal_order_does_not_erase_new_search_each_turn():
    from app.sales.dialogue_phase import reconcile_checkout_context, resolve_dialogue_phase
    from app.memory.working_memory import build_working_memory
    from app.models import AgentResult
    state = CommerceConversationState(order_id='historical', order_status_group='shipped',
        order_payment_status='unknown', dialogue_phase='checkout')
    state = reconcile_checkout_context(state)
    state.active_preferences = {'subject_brand': 'Orient', 'budget_max': 2500}
    restored = CommerceConversationState.model_validate(state.model_dump())
    restored = reconcile_checkout_context(restored)
    assert restored.active_preferences == state.active_preferences
    assert resolve_dialogue_phase(restored, {}, AgentResult(reply_text='Qual estilo?', intent='commerce')) != 'checkout'
    memory = build_working_memory(restored)
    assert not memory['has_open_order'] and not memory['payment_pending']


def test_new_search_does_not_inherit_prior_brand_or_budget():
    state = CommerceConversationState(active_preferences={'subject_brand': 'Citizen',
        'budget_max': 3000, 'budget_payment_basis': 'pix'})
    i = interpretation(subject={}, references_previous_context=False)
    reconcile_search_turn(i, state, 'Quero um relógio')
    assert i.subject.brand is None
    assert i.preferences.budget_max is None
    assert i.payment_method_preference is None


def test_calibre_is_not_a_product_reference():
    i = interpretation(subject={'brand': 'Citizen', 'model': '8204', 'reference': '8204'})
    reconcile_search_turn(i, CommerceConversationState(), 'Preto, mineral, movimento 8204.')
    assert i.subject.model is None and i.subject.reference is None
    assert 'calibre 8204' in i.preferences.attributes


def test_published_unconfirmed_reply_is_not_rejudged_as_api_failure():
    from app.sales.adaptive_discovery import unconfirmed_result
    from app.verify.response_critique import apply_fast_deterministic_critique
    from app.models import IncomingMessage
    i = interpretation(preferences={'style': 'social', 'budget_max': 2500})
    result = unconfirmed_result(i)
    unchanged, _, reason = apply_fast_deterministic_critique(
        incoming=IncomingMessage(text='Quero um clássico até 2500'), result=result)
    assert unchanged is result
    assert reason == 'published_discovery_unconfirmed'


def test_poisoned_name_is_removed_but_actual_name_survives():
    poisoned = {'qualification_slots': {'customer_name': 'uso próprio'},
                'attributes': ['qual:name:uso próprio']}
    repaired = merge_persisted_qualification_slots(poisoned, poisoned)
    assert 'customer_name' not in repaired['qualification_slots']
    assert repaired['attributes'] == []
    actual = {'attributes': ['qual:name:Tironi']}
    assert merge_persisted_qualification_slots(actual, {})['qualification_slots']['customer_name'] == 'Tironi'
