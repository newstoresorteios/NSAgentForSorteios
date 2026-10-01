"""Read-only suggestions for an unresolved Story, with operator references first."""
from urllib.parse import urlparse

from app.catalog.retrieval.text import fold_text
from app.catalog.media.product_media import official_product_url


def safe_product_url(url):
    parsed = urlparse(str(url or ''))
    return (parsed.scheme == 'https' and parsed.netloc in {
        'www.newstorerj.com', 'newstorerj.com',
        'www.newstorerj.com.br', 'newstorerj.com.br',
    } and bool(parsed.path.strip('/')))


async def lookup_story_options(ref, *, execute_tool=None):
    """Never turn a configured link or search candidate into a confirmed Story SKU."""
    from app.persona.persona_runtime import get_persona_runtime
    from app.stories.story_highlight_references import current_workspace_reference
    from app.sales.ready_delivery_context import terms
    from app.ops.observability import log_event

    query = ref['catalog_query']
    runtime = get_persona_runtime()
    reference = current_workspace_reference(query, tenant_id=runtime.tenant_id)
    check = {'reference_checked': True, 'reference_id': None, 'catalog_checked': False,
             'products': [], 'source': None}
    if reference and safe_product_url(reference.product_url):
        # A brand hit must not override a color/size explicitly requested later.
        requested = set(terms(query)) - {'aco', 'automatico', 'automatic'}
        evidence = set(terms(reference.name + ' ' + urlparse(reference.product_url).path))
        if requested.issubset(evidence):
            check.update(reference_id=reference.id, source='story_highlight_references',
                         products=[{'name': reference.name, 'url': reference.product_url}])
            return check
    if execute_tool is None:
        return check
    check['catalog_checked'] = True
    try:
        payload = await execute_tool('search_products', {'name': query, 'limit': 10, 'page': 1})
    except Exception as exc:
        log_event('story.followup_catalog_failed', {'error_type': type(exc).__name__})
        check['catalog_failed'] = True
        return check
    if not isinstance(payload, dict) or payload.get('error'):
        check['catalog_failed'] = True
        return check
    required = set(terms(query))
    seen = set()
    for product in payload.get('products') or []:
        if not isinstance(product, dict) or not product.get('name'):
            continue
        url = official_product_url(product)
        searchable = fold_text(' '.join(str(product.get(k) or '') for k in
                                       ('name', 'brand', 'model', 'reference')))
        if (not safe_product_url(url) or url in seen or
                not required.issubset(set(terms(searchable))) or
                product.get('available') not in (True, 1, '1')):
            continue
        seen.add(url)
        check['products'].append({'name': product['name'], 'url': url})
        if len(check['products']) == 3:
            break
    if check['products']:
        check['source'] = 'tray_catalog'
    return check


def present_story_options(result, check):
    label = ('nas referências dos Stories' if check['source'] == 'story_highlight_references'
             else 'no catálogo')
    products = check['products']
    reply = f'Encontrei {label}:\n' + '\n'.join(
        f"• {p['name']}\n{p['url']}" for p in products)
    question = ('A foto do anúncio corresponde ao relógio que você procura?' if len(products) == 1
                else 'Qual dessas opções corresponde ao relógio que você procura?')
    reply += '\nAinda não confirmei a versão exata do Story. ' + question
    result.reply_text = reply
    # Only names and URLs are evidence here; no price, inventory or checkout IDs.
    result.response_metadata.setdefault('verified_facts', {})['story_catalog_options'] = products
    result.response_metadata.update(story_clarification_reply=reply, story_catalog_check=check)
    return result
