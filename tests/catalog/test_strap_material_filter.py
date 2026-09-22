from app.catalog.retrieval.hard_filter import hard_filter_products
from app.models import SalesInterpretation


def test_explicit_steel_bracelet_rejects_same_brand_leather_watch():
    request = SalesInterpretation(
        domain='commerce', goal='find', confidence=.99,
        needs_clarification=False,
        references_previous_context=False,
        subject={'brand':'Baltic','product_type':'relógio'},
        preferences={
            'material':'aço',
            'attributes':['required_strap_material:aço'],
        },
    )
    products = [
        {
            'id':'11515', 'brand':'Baltic',
            'name':'Relógio Baltic MR01 Automático Salmão Marrom',
            'description':'Caixa em aço inoxidável e pulseira de couro marrom',
            'price':7699.99, 'available':True,
        },
        {
            'id':'14738', 'brand':'Baltic',
            'name':'Relógio Baltic Aquascaphe MK2 37mm',
            'variants':[
                {'id':'1714','name':'Borracha'},
                {'id':'1716','name':'Aço inoxidável'},
            ],
            'price':9299.99, 'available':True,
        },
    ]

    kept = hard_filter_products(
        products,
        request,
        mode='recommendation',
        message_text='Quero o Baltic com pulseira de aço',
    )

    assert [product['id'] for product in kept] == ['14738']
