import pytest

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.models import SalesInterpretation


def intent(product_type='relógio'):
    return SalesInterpretation(domain='commerce',goal='find',subject={'product_type':product_type},
                               confidence=.99,references_previous_context=False,needs_clarification=False)


@pytest.mark.parametrize('name',[
    'Pulseira Hamilton American Classic Mesh H6053841041 20 mm',
    'Watch Winder Caixa de Suporte Rotativo Para Relógios Automáticos',
    'Kit De Reparo Relojoeiro',
])
def test_accessories_cannot_consume_watch_detail_candidates(name):
    product={'id':'1','name':name,'price':100,'available':True}
    assert hard_filter_products([product],intent(),mode='recommendation',allow_unknown_features=True)==[]


def test_watch_with_strap_description_and_explicit_strap_search_are_preserved():
    watch={'id':'1','name':'Relógio Orient automático pulseira de aço','price':100,'available':True}
    strap={'id':'2','name':'Pulseira Hamilton 20 mm','price':100,'available':True}
    assert hard_filter_products([watch],intent(),mode='recommendation')==[watch]
    assert hard_filter_products([strap],intent('pulseira'),mode='recommendation')==[strap]
