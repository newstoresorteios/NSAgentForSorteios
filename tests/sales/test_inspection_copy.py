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


def test_product_title_does_not_turn_availability_into_a_specification_question():
    original = result(name='Seiko Presage Automático SSA459J1', available=True, availability='30 dias úteis',
                      mechanism='automático com corda manual')
    answer = complete_inspection_copy(original, interpretation(goal='inspect'),
                                     'Tem Seiko Presage Automático SSA459J1 disponível?')
    assert 'disponível no catálogo' in answer.reply_text
    assert '30 dias úteis' in answer.reply_text
    assert 'Movimento:' not in answer.reply_text


def test_exact_lookup_answers_timing_and_keeps_identity_even_when_routed_as_find():
    original = result(name='Certina DS Action', reference='ABC-123', availability='Disponível em 30 dias úteis')
    original.response_metadata = {}
    answer = complete_inspection_copy(original, interpretation(goal='find', subject={'reference': 'ABC-123'}),
        'Quero saber se o prazo indicado no site é de entrega ou envio do produto.')
    assert 'Certina DS Action' in answer.reply_text
    assert '30 dias úteis' in answer.reply_text
    assert 'nem uma data exata de postagem' in answer.reply_text
    assert 'não confirma a data de entrega' in answer.reply_text
    assert answer.response_metadata['identity_inspection']


def test_quoted_available_in_thirty_days_is_timing_not_a_new_inventory_search():
    answer = complete_inspection_copy(result(name='Certina DS Action', availability='Disponível em 30 dias úteis'),
        interpretation(goal='inspect'), 'Disponível em 30 dias úteis quer dizer que recebo ou que será enviado nesse prazo?')
    assert 'nem uma data exata de postagem' in answer.reply_text
    assert 'não confirma a data de entrega' in answer.reply_text


def test_misunderstood_timing_rephrases_explanation_without_repeating_cep_question():
    item = interpretation(goal='inspect')
    item.conversation_feedback = 'misunderstood'
    answer = complete_inspection_copy(result(name='Certina', availability='Disponível em 30 dias úteis'),
        item, 'Não entendi, esse prazo é de entrega ou envio?')
    assert 'Vou separar as etapas' in answer.reply_text
    assert 'postagem' in answer.reply_text and 'recebido' in answer.reply_text
    assert 'qual é o seu CEP' not in answer.reply_text


def test_automatic_and_manual_winding_are_distinct_compatible_facts():
    answer = complete_inspection_copy(result(mechanism='automático com corda manual'),
                                     interpretation(goal='inspect'), 'Qual o movimento?')
    assert 'automático com capacidade de corda manual' in answer.reply_text
    facts = answer.response_metadata['technical_facts']['movement']
    assert facts['movement_types'] == ['automatic']
    assert facts['manual_winding'] is True and facts['conflicting_types'] is False


def test_conflicting_movement_types_are_still_reported_and_capability_not_invented():
    answer = complete_inspection_copy(result(mechanism='automático e quartzo'),
                                     interpretation(goal='inspect'), 'Qual o movimento?')
    assert 'informações diferentes' in answer.reply_text
    assert answer.response_metadata['technical_facts']['movement']['manual_winding'] is None
