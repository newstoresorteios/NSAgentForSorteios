"""Published policy evidence shared by independent response reviewers."""
from decimal import Decimal, ROUND_HALF_UP
from app.catalog.retrieval.price import resolve_commercial_price


def persona_evidence(products, persona=None):
    if persona is None:
        from app.persona.persona_runtime import get_persona_runtime
        persona=get_persona_runtime()
    if not persona or not persona.enabled:
        return {}
    percent=Decimal(str(persona.pix_discount_percent))
    derived=[]
    for product in products or []:
        price=resolve_commercial_price(product,require_positive=True).amount
        if price is not None and 0<=percent<100:
            derived.append({'product_id':str(product.get('id') or product.get('product_id')),
                'catalog_price':str(price),'pix_discount_percent':str(percent),
                'derived_pix_price':str((price*(Decimal(100)-percent)/Decimal(100)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
                'source':'published_persona_discount_applied_to_catalog_price'})
    return {'policy':persona.flow_params_dict(),
        'instructions':persona.active_persona.instructions if persona.active_persona else '',
        'derived_informational_prices':derived}
