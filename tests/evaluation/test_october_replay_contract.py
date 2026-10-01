from app.evaluation.simulator import CommerceSimulator
from app.llm.turn_understanding import TurnUnderstanding, turn_understanding_to_sales
from tests.evaluation.october_annotations import get_annotation
from tests.evaluation.october_replay import synthetic_commerce


def test_simulated_inventory_filter_does_not_return_backordered_catalog_items():
    simulator = CommerceSimulator(synthetic_commerce())
    data = simulator.execute('search_products', {'available': True, 'available_in_store': True})
    assert {p['id'] for p in data['products']} == {'91003'}
    assert data['paging']['total'] == 1


def test_wedding_annotation_keeps_declared_occasion_without_inventing_style():
    parsed = turn_understanding_to_sales(TurnUnderstanding.model_validate(get_annotation('1150')))
    assert parsed.subject.product_type == 'relógio'
    assert parsed.preferences.occasion == 'casamento'
    assert parsed.preferences.style is None
    assert parsed.preferences.delivery_deadline_text == 'próximo final de semana'


def test_bare_document_annotation_does_not_invent_order_lookup_intent():
    parsed = TurnUnderstanding.model_validate(get_annotation('1174'))
    assert parsed.clarification_required
    assert parsed.requested_action.kind == 'none'
