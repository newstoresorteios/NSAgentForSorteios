from app.models import AgentResult
from app.sales.inspection_copy import complete_inspection_copy
from tests.sales.test_contextual_discovery import interpretation


def result(**product):
    return AgentResult(reply_text='Relógio confirmado, R$ 2.000 no Pix.', intent='commerce',
        response_metadata={'identity_inspection': True},
        commercial_data={'products': [{'id': '1', '_revalidated': True, **product}]})


def test_inspection_answers_glass_movement_and_delivery_without_guessing():
    original = result(description='Movimento automático e vidro de safira.', availability='30 dias úteis')
    reply = complete_inspection_copy(original, interpretation(goal='inspect'),
        'Confirme vidro, movimento e prazo. Chega amanhã?').reply_text
    assert 'safira' in reply.lower()
    assert 'autom' in reply.lower()
    assert '30 dias úteis' in reply
    assert 'Não posso garantir chegada amanhã' in reply
    assert original.reply_text == 'Relógio confirmado, R$ 2.000 no Pix.'


def test_missing_facts_are_explicit_not_guessed_from_customer_question():
    reply = complete_inspection_copy(result(), interpretation(goal='inspect'),
        'É azul, mineral, 40mm?').reply_text
    assert 'não confirmado' in reply
    assert 'Diâmetro da caixa: não confirmado' in reply
    assert 'Vidro: não confirmado' in reply
    assert 'Cor do mostrador: não confirmada' in reply


def test_cached_data_cannot_generate_confirmed_specs():
    original = result(_revalidated=False, crystal='safira')
    assert complete_inspection_copy(original, interpretation(goal='inspect'), 'Qual vidro?') is original


def test_conflicting_size_is_not_silently_resolved():
    original = result(case_size=40)
    original.commercial_data['catalog_discrepancies'] = [
        {'field': 'case_size_mm', 'description': '40', 'summary': '42'}]
    reply = complete_inspection_copy(original, interpretation(goal='inspect'), 'Qual o tamanho?').reply_text
    assert '40 mm' in reply and '42 mm' in reply and 'divergente' in reply


def test_labelled_html_specs_are_not_treated_as_absent():
    original = result(description='<div><span>COR DA CAIXA: Prata</span></div>'
        '<div><span>COR DE DISCAGEM: Preto</span></div>'
        '<div><span>MATERIAL DE PULSEIRA: Borracha</span></div>'
        '<div><span>MOTOR: Citizen Caliber 8204</span></div>')
    reply = complete_inspection_copy(original, interpretation(goal='inspect'), 'Confirme cada característica').reply_text
    assert 'Cor do mostrador: Preto.' in reply
    assert 'Pulseira: Borracha.' in reply
    assert 'Calibre/motor: Citizen Caliber 8204.' in reply
    assert 'Cor do mostrador: Prata' not in reply


def test_explicit_identity_inspection_does_not_need_contextual_route_marker():
    original = result(reference='ABC-123', crystal='mineral')
    original.response_metadata = {}
    i = interpretation(goal='inspect', subject={'reference': 'ABC-123'})
    answer = complete_inspection_copy(original, i, 'Qual o vidro?')
    assert answer.response_metadata['identity_inspection'] is True
    assert 'mineral' in answer.reply_text
