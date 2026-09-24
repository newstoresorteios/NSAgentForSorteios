import pytest

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.sales.qualification_enrichment import apply_qualification_answer
from tests.sales.test_contextual_discovery import interpretation


@pytest.mark.parametrize('requested', ['clássico', 'classic', 'dress', 'social'])
@pytest.mark.parametrize('catalog_style', ['Clássico', 'Classic', 'Dress', 'Social'])
def test_equivalent_styles_preserve_results(requested, catalog_style):
    i = interpretation(preferences={'style': requested})
    rows = [dict(id='1', name='Hamilton modelo X', brand='Hamilton',
                 style=catalog_style, current_price=2000, available=True),
            dict(id='2', name='Hamilton modelo Y', brand='Hamilton',
                 style='Mergulho', current_price=2000, available=True)]
    assert [p['id'] for p in hard_filter_products(rows, i, mode='recommendation')] == ['1']


@pytest.mark.parametrize('text,expected', [
    ('não é para presente, é para mim', 'self'),
    ('não é para mim, é para presente', 'gift'),
    ('não quero presentear', None),
    ('para mim ou para presente', None),
    ('presente', 'gift'),
    ('para mim', 'self'),
])
def test_purpose_respects_negation_and_ambiguity(text, expected):
    i = interpretation()
    apply_qualification_answer(i, text, 'purchase_purpose')
    assert i.preferences.attributes == ([f'qual:purchase_purpose:{expected}'] if expected else [])


def test_purpose_can_be_relaxed_and_selected_again():
    i = interpretation()
    for text in ('presente', 'tanto faz'):
        apply_qualification_answer(i, text, 'purchase_purpose')
    assert not i.preferences.attributes
    assert 'purchase_purpose' in i.preferences.explicit_no_preferences
    apply_qualification_answer(i, 'para mim', 'purchase_purpose')
    assert i.preferences.attributes == ['qual:purchase_purpose:self']
    assert 'purchase_purpose' not in i.preferences.explicit_no_preferences
