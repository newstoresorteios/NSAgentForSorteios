"""Evidence-only fallback for explicit questions about a live, identified SKU."""
import re
import html

from app.catalog.retrieval.text import fold_text
from app.catalog.specs.catalog_specs import extract_case_size_mm, extract_water_resistance_m
from app.catalog.specs.requirements import feature_evidence, feature_rules


def labelled_fact(product, keys, labels):
    """Read labelled catalog facts without mixing case, dial and strap fields."""
    values = [str(product[k]).strip() for k in keys if product.get(k)]
    properties = product.get('properties')
    if isinstance(properties, dict):
        for key, value in properties.items():
            if fold_text(key) in labels:
                values.extend(str(v).strip() for v in (value if isinstance(value, list) else [value]) if v)
    description = str(product.get('description') or '')
    description = re.sub(r'</(?:div|p|li|tr|h[1-6])\s*>|<br\s*/?>', '\n', description, flags=re.I)
    description = html.unescape(re.sub(r'<[^>]+>', '', description))
    for line in description.splitlines():
        key, separator, value = line.partition(':')
        if separator and fold_text(key).strip() in labels and value.strip():
            values.append(value.strip())
    unique = {fold_text(value): value for value in values}
    if len(unique) > 1:
        return ' / '.join(unique.values()) + ' (valores divergentes no cadastro)'
    return next(iter(unique.values()), None)


def complete_inspection_copy(result, interpretation, text):
    if interpretation is None:
        return result
    products = (result.commercial_data or {}).get('products') or []
    if len(products) != 1 or not products[0].get('_revalidated'):
        return result
    product = products[0]
    identity = result.response_metadata.get('identity_inspection') or (
        interpretation.goal == 'inspect' and interpretation.subject.reference
        and fold_text(interpretation.subject.reference) == fold_text(product.get('reference')))
    if not identity:
        return result
    query = fold_text(text)
    all_details = bool(re.search(r'caracteristica|ficha tecnica|todos os detalhes', query))
    requested = {
        'crystal': all_details or bool(re.search(r'vidro|cristal|safira|mineral|hardlex', query)),
        'mechanism': all_details or bool(re.search(r'movimento|mecanismo|automatic|quartzo|eco.drive|calibre', query)),
    }
    lines = []
    rules = feature_rules()
    for field, wanted in requested.items():
        if not wanted:
            continue
        fact = feature_evidence(product, {field: ''})['facts'][0]
        labels = {r['value']: r['label'] for r in rules if r['field'] == field}
        observed = [labels.get(v, v) for v in fact['observed']]
        label = 'Vidro' if field == 'crystal' else 'Movimento'
        value = ', '.join(observed) if observed else 'não confirmado na ficha consultada'
        if len(observed) > 1:
            value += ' (há informações diferentes no cadastro; não confirmo uma única especificação)'
        lines.append(f'{label}: {value}.')
    if all_details or re.search(r'tamanho|diametro|caixa|\d\s*mm\b|\bmm\b', query):
        discrepancies = (result.commercial_data or {}).get('catalog_discrepancies') or []
        conflict = next((v for v in discrepancies if v.get('field') == 'case_size_mm'), None)
        if conflict:
            lines.append(f"Diâmetro divergente no catálogo: {conflict['description']} mm na descrição e {conflict['summary']} mm nas características; não confirmo uma medida única.")
        else:
            size = extract_case_size_mm(product)
            lines.append(f'Diâmetro da caixa: {size} mm.' if size else 'Diâmetro da caixa: não confirmado na ficha consultada.')
    if all_details or re.search(r'agua|resistencia|\bmetros\b|\batm\b', query):
        water = extract_water_resistance_m(product)
        lines.append(f'Resistência à água informada: {water} metros.' if water else 'Resistência à água: não confirmada na ficha consultada.')
    if all_details or re.search(r'cor|mostrador|azul|preto|verde|vermelho', query):
        # Accessory colors are not evidence of dial color.
        color = labelled_fact(product, ('dial_color',),
            {'cor do mostrador', 'cor de discagem', 'cor do dial', 'dial color'})
        lines.append(f'Cor do mostrador: {color}.' if color else 'Cor do mostrador: não confirmada em campo específico da ficha; confira a identificação do produto acima.')
    if all_details or re.search(r'pulseira|bracelete', query):
        strap = labelled_fact(product, ('strap_material', 'bracelet_material'),
            {'material da pulseira', 'material de pulseira', 'material do bracelete', 'strap material'})
        lines.append(f'Pulseira: {strap}.' if strap else 'Material da pulseira: não confirmado em campo específico da ficha.')
    if all_details or re.search(r'calibre|caliber|motor|movimento\s+\d', query):
        calibre = labelled_fact(product, ('caliber', 'calibre'), {'motor', 'calibre', 'caliber'})
        lines.append(f'Calibre/motor: {calibre}.' if calibre else 'Calibre/motor: não confirmado na ficha consultada.')
    if re.search(r'prazo|entrega|amanha|chega', query):
        literal = product.get('availability')
        if isinstance(literal, str) and literal.strip():
            lines.append(f'Disponibilidade informada no catálogo: {literal.strip()}.')
        lines.append('A data de entrega no seu endereço não está confirmada; depende da cotação para o CEP. Não posso garantir chegada amanhã.'
                     if 'amanha' in query else 'O prazo de entrega no seu endereço depende da cotação para o CEP; disponibilidade do produto não é confirmação da data de chegada.')
    if not lines:
        return result
    from app.commerce.commerce_router import _product_result
    from app.catalog.retrieval.price import budget_price
    from app.catalog.retrieval.tokens import product_conflicts_dial_color
    base = _product_result('product_search', [product]).reply_text
    price = budget_price(product, interpretation.payment_method_preference)
    ceiling = interpretation.preferences.budget_max
    if ceiling is not None and price is not None and price > ceiling:
        base = 'Não cabe no orçamento informado.\n' + base
    color = interpretation.preferences.color
    if color and product_conflicts_dial_color(product, (color,)):
        base = f'Esta referência não atende ao critério de mostrador {color}.\n' + base
    updated = result.model_copy(deep=True)
    updated.reply_text = base + '\n' + '\n'.join(lines)
    updated.response_metadata['inspection_requested_facts_covered'] = True
    updated.response_metadata['identity_inspection'] = True
    return updated
