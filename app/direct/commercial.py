"""Read-only quotations with server-resolved products and prices."""
import re

from app.catalog.retrieval.price import resolve_commercial_price


async def quote_shipping(adapter, args, incoming, history, known_ids):
    if args.product_id not in known_ids:
        return {'ok': False, 'error': 'known_admin_product_required',
                'instruction': 'Não cotar produto público da pronta entrega usando ID de outra loja.'}
    zipcode = args.zipcode.replace('-', '')
    customer_text = '\n'.join([incoming.text or ''] + [h['content'] for h in history[-20:] if h.get('role') == 'user'])
    if zipcode not in {m.replace('-', '') for m in re.findall(r'(?<!\d)\d{5}-?\d{3}(?!\d)', customer_text)}:
        return {'ok': False, 'error': 'customer_zipcode_required'}
    raw = await adapter.get_product(args.product_id)
    product = raw.get('product') or raw
    if str(product.get('id')) != args.product_id:
        return {'ok': False, 'error': 'product_identity_mismatch'}
    variant = None
    if args.variant_id:
        raw_variant = await adapter.get_product_variant(args.variant_id)
        variant = raw_variant.get('variant') or raw_variant
        if str(variant.get('product_id')) != args.product_id or str(variant.get('id')) != args.variant_id:
            return {'ok': False, 'error': 'variant_identity_mismatch'}
    elif str(product.get('has_variation')).lower() in {'1', 'true'}:
        return {'ok': False, 'error': 'variant_required'}
    price = resolve_commercial_price(variant or product, require_positive=True).amount
    if price is None:
        return {'ok': False, 'error': 'current_price_unavailable'}
    item = {'product_id': int(args.product_id), 'price': str(price), 'quantity': args.quantity}
    if args.variant_id:
        item['variant_id'] = int(args.variant_id)
    result = await adapter.quote_shipping(zipcode=zipcode, products=[item])
    fields = {'name', 'price', 'min_period', 'max_period', 'estimated_delivery_date', 'information'}
    return {'ok': bool(result.get('success')), 'options': [
        {k: v for k, v in option.items() if k in fields} for option in result.get('options', [])],
        'instruction': 'Cotação estimada, não garantia de chegada. Não cria pedido nem reserva estoque.'}
