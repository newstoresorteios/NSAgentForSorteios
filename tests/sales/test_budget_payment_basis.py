import pytest
from app.models import AgentResult
from app.sales.answer_council import check_pedido, check_fatos
from app.sales.turn_contract import TurnContract
from app.verify.factual_validator import validate_factual_response
from tests.verify.test_factual_validator import _decision


@pytest.mark.parametrize('basis,pix,allowed', [('pix', 2889.99, True),
    ('card', 2889.99, False), (None, 2889.99, False), ('pix', 3144.99, False)])
def test_budget_validators_share_payment_basis(basis, pix, allowed):
    result = AgentResult(reply_text=f'Preço no Pix: R$ {pix:.2f}.', intent='commerce',
        commercial_data={'products': [{'id': '1', 'name': 'Citizen', 'current_price': 3399.99,
            'pix_price': pix, '_revalidated': True, '_factual_source': 'tray_live'}]},
        response_metadata={'used_tray': True, 'domain': 'commerce', 'hard_budget_max': 3000,
            'presented_products': True, 'interpretation': {'payment_method_preference': basis}})
    contract = TurnContract(budget_max=3000)
    assert ('presented_over_budget' not in check_pedido(result, contract).issues) == allowed
    assert ('fact_price_over_budget' not in check_fatos(result, contract).issues) == allowed
    report = validate_factual_response(result, decision=_decision(result))
    assert (not any(v.reason == 'presented_over_budget' for v in report.violations)) == allowed


def test_final_delivery_keeps_pix_and_specs_after_projection_drops_derived_price(monkeypatch):
    from app.commerce.commerce_context import CommerceConversationState
    from app.models import IncomingMessage
    from app.verify.final_response import finalize_response
    from tests.sales.test_contextual_discovery import interpretation
    monkeypatch.setattr('app.commerce.commerce_router._pix_discount_percent', lambda: 15)
    i = interpretation(goal='inspect', subject={'brand': 'Citizen', 'reference': 'NY0120-01EE'},
        preferences={'budget_max': 3000}, payment_method_preference='pix', reference_type='explicit_product')
    product = {'id': '1', 'name': 'Citizen NY0120-01EE', 'reference': 'NY0120-01EE',
        'brand': 'Citizen', 'current_price': 3399.99, 'price': 3399.99,
        'available': '1', '_revalidated': True, '_factual_source': 'tray_live',
        'description': '<div>MOTOR: Citizen Caliber 8204</div><div>CRISTAL: Mineral</div>',
        'url': 'https://www.newstorerj.com.br/relogio'}
    # Actual failure: after projection there is no derived pix_price field.
    result = AgentResult(reply_text='Achei o Citizen.', intent='commerce',
        commercial_data={'products': [product]}, response_metadata={
            'interpretation': i.model_dump(), 'identity_inspection': True,
            'used_tray': True, 'presented_products': True})
    final, _ = finalize_response(result, incoming=IncomingMessage(text='Confirme cada característica e o preço no Pix.'),
        interpretation=i, previous_state=CommerceConversationState())
    assert len(final.commercial_data['products']) == 1
    assert final.safety_reason != 'answer_council_blocked'
    assert '2.889,99' in final.reply_text
    assert '8204' in final.reply_text and 'mineral' in final.reply_text
    assert not final.response_metadata['final_response_validation']['rejected_issues']
