import pytest

from app.sales.responder import recommendation_identifies_candidate


@pytest.mark.parametrize("text,valid", [
    ("Encontrei opções para você.", False),
    ("Citizen é uma ótima marca.", False),
    ("O preço é 641 reais.", False),
    ("Veja o **Relogio Citizen Azul**.", True),
    ("Sugiro a referência BN0151-09L.", True),
    ("Veja https://example.test/watch/citizen-azul", True),
    ("Sugiro BN0151-09LX.", False),
])
def test_candidate_identity_requires_catalog_name_reference_or_url(text, valid):
    products = [{"id": "641", "brand": "Citizen", "name": "Relógio Citizen Azul",
                 "reference": "BN0151-09L", "product_url": "https://example.test/watch/citizen-azul"}]
    assert recommendation_identifies_candidate(text, products) is valid
