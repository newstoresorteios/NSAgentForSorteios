"""Deterministic ready-delivery lookup; never mix IDs from the two stores."""
from urllib.parse import urlparse

from app.models import AgentResult
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.sales.ready_delivery_context import resolve_query, scoped_context

SOURCE = 'https://www.newstorerj.com/pronta-entrega'


def enabled():
    from app.persona.site_knowledge import STORE_PRONTA_ENTREGA_URL
    return urlparse(str(STORE_PRONTA_ENTREGA_URL() or '')).hostname in {'www.newstorerj.com', 'newstorerj.com'}


async def lookup(query):
    try:
        data = await TrayAdapterClient().search_ready_delivery(query)
    except TrayAdapterError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    products = data.get('products') or []
    complete = data.get('success') is True and data.get('complete') is True and isinstance(products, list)
    available = [p for p in products if isinstance(p, dict)
                 and p.get('listedAvailable') is True and p.get('name')
                 and urlparse(str(p.get('url') or '')).hostname == 'www.newstorerj.com'
                 and str(p.get('url')).startswith('https://')] if complete else []
    # Public-listing evidence never enters the .com.br checkout/price payload.
    evidence = {'source': SOURCE, 'query': query[:500], 'complete': complete,
                'checked_at': data.get('checkedAt'), 'stock_confirmed': False,
                'result_count': len(available), 'requires_model': bool(data.get('requiresModel')),
                'products': [{k: p.get(k) for k in ('name', 'reference', 'url')} for p in available[:3]]}
    from app.ops.observability import log_event
    log_event('catalog.ready_delivery_lookup', {k: v for k, v in evidence.items() if k != 'products'})
    return evidence


def listing(evidence):
    products = evidence['products']
    prefix = ('Encontrei esta opção anunciada como disponível' if len(products) == 1
              else 'Encontrei estas opções anunciadas como disponíveis')
    return prefix + ' na nossa lista de pronta entrega:\n' + '\n'.join(
        f"• {p['name']}\n{p['url']}" for p in products)


def public_listing_evidence(result):
    """Return verified second-store evidence without importing first-store facts."""
    metadata = result.response_metadata or {}
    evidence = metadata.get('ready_delivery_check')
    if (metadata.get('response_source') != 'ready_delivery_storefront'
            or metadata.get('catalog_source') != SOURCE or not isinstance(evidence, dict)
            or evidence.get('source') != SOURCE or evidence.get('complete') is not True):
        return None
    products = evidence.get('products')
    if not isinstance(products, list) or not products:
        return None
    for product in products:
        if not isinstance(product, dict) or not product.get('name'):
            return None
        url = urlparse(str(product.get('url') or ''))
        if url.scheme != 'https' or url.netloc != 'www.newstorerj.com':
            return None
    return evidence


async def try_ready_delivery(message, state=None):
    query = resolve_query(message, state)
    if not query or not enabled():
        return None
    evidence = await lookup(query)
    metadata = {'domain': 'commerce', 'response_source': 'ready_delivery_storefront',
                'catalog_source': SOURCE, 'ready_delivery_check': evidence,
                'ready_delivery_context': scoped_context(message, query),
                'clear_active_product': True, 'clear_presented_products': True}
    if not evidence['complete']:
        reply = 'Não consegui consultar nossa lista de pronta entrega agora. Isso não significa que o relógio esteja esgotado. Posso encaminhar para o comercial verificar?'
    elif evidence['requires_model']:
        reply = 'Qual modelo, referência e cor você procura à pronta entrega? Vou consultar a lista da nossa loja: ' + SOURCE
    elif evidence['products']:
        reply = listing(evidence) + '\nO estoque final precisa ser confirmado antes de fechar. Quer que o comercial confirme a peça e te ajude com a compra?'
    else:
        reply = 'Não encontrei essa combinação de modelo e cor disponível na nossa lista atual de pronta entrega. Posso encaminhar para o comercial verificar essa peça para você?'
    if not evidence['requires_model']:
        metadata['handoff'] = {'offer': True, 'required': False}
    return AgentResult(reply_text=reply, intent='sales', response_metadata=metadata)


async def try_reference_ready_delivery(message, interpretation):
    """Resolve an explicit shopping identity in the preferred storefront first."""
    from app.catalog.retrieval.tokens import extract_reference_code
    from app.catalog.retrieval.text import fold_text
    from app.persona.persona_runtime import get_persona_runtime
    from app.sales.ready_delivery_context import BLOCKED, EXPLICIT
    import re

    runtime = get_persona_runtime()
    if (interpretation is None or interpretation.domain != 'commerce'
            or interpretation.goal not in {'find', 'inspect'}
            or not runtime or not runtime.prefer_ready_stock or not scoped_context(message, '')
            or not enabled() or message.image_url or message.instagram_story):
        return None
    if any((interpretation.purchase_action, interpretation.checkout_action,
            interpretation.order_action, interpretation.shipping_action,
            interpretation.payment_action, interpretation.image_request)):
        return None
    text = fold_text(message.text or '')
    if (re.search(BLOCKED, text) or re.search(r'\bnao\b.{0,25}' + EXPLICIT, text)
            or 'sob encomenda' in text or 'seminovo' in text or 'usado' in text):
        return None
    reference = extract_reference_code(message.text)
    if not reference:
        return None
    if extract_reference_code(str(message.text).replace(reference, '', 1)):
        return None
    subject = interpretation.subject
    if subject.reference and reference.casefold() != subject.reference.strip().casefold():
        return None
    # A factual/technical inspection must still reach the normal product sheet.
    identity_words = set(re.findall(r'[a-z0-9]+', fold_text(' '.join(
        str(value or '') for value in (subject.brand, subject.model, reference)))))
    bare_identity = set(re.findall(r'[a-z0-9]+', text)).issubset(identity_words)
    needed = set(interpretation.information_needed)
    if interpretation.goal == 'inspect' and not (
            bare_identity or needed and needed.issubset({'price', 'inventory'})):
        return None
    evidence = await lookup(reference)
    if not evidence['complete']:
        return None
    # Do not replace the requested SKU with a family match or a foreign store ID.
    evidence['products'] = [p for p in evidence['products']
                            if str(p.get('reference') or '').strip().casefold() == reference.casefold()]
    evidence['result_count'] = len(evidence['products'])
    if not evidence['products']:
        return None
    return AgentResult(
        reply_text=listing(evidence) + '\nConfira o preço e as condições atuais e compre pelo link do anúncio.',
        intent='sales', response_metadata={
            'domain': 'commerce', 'response_source': 'ready_delivery_storefront',
            'catalog_source': SOURCE, 'ready_delivery_check': evidence,
            'ready_delivery_context': scoped_context(message, reference),
            'ready_delivery_exact_reference': reference,
            'clear_active_product': True, 'clear_presented_products': True,
        },
    )


async def enrich_story_ready_delivery(message, result, *, execute_tool=None):
    """Search the second storefront without upgrading a visual guess to a match."""
    ref = result.response_metadata.get('last_story_product')
    scope = scoped_context(message, '')
    from app.stories.story_selection import reference_in_workspace
    from app.persona.persona_runtime import get_persona_runtime
    if (not isinstance(ref, dict) or ref.get('match_status') not in {'ambiguous', 'not_found'}
            or not ref.get('catalog_query') or not scope
            or not reference_in_workspace(ref, get_persona_runtime())
            or any(ref.get(k) != scope[k] for k in ('conversation_id', 'sender_key'))):
        return result
    from app.stories.story_followup_catalog import lookup_story_options, present_story_options
    catalog = await lookup_story_options(ref, execute_tool=execute_tool)
    result.response_metadata['story_catalog_check'] = catalog
    if catalog['products']:
        return present_story_options(result, catalog)
    if not enabled():
        return result
    evidence = await lookup(ref['catalog_query'])
    selected = ref.get('selected_option')
    show_options = bool(selected or result.response_metadata.get('response_source') == 'instagram_story_followup')
    if not evidence['complete']:
        prefix = 'Não consegui consultar a lista de pronta entrega agora; isso não significa que a peça esteja esgotada.'
    elif show_options and evidence['products']:
        prefix = listing(evidence)
        question = ('A foto do anúncio corresponde ao que você escolheu?' if len(evidence['products']) == 1
                    else 'Qual dessas opções corresponde ao relógio que você escolheu?')
        prefix += ('\nAinda não confirmei que corresponde exatamente ao relógio do Story. '
                   + question + ' O estoque final precisa ser confirmado antes de fechar.')
    elif evidence['products']:
        prefix = 'Consultei também a lista de pronta entrega e encontrei opções dessa linha, mas ainda preciso confirmar qual é a do Story.'
    else:
        prefix = ('Consultei a lista de pronta entrega, mas não confirmei uma correspondência para o relógio do Story. '
                  'Isso não confirma que a peça esteja esgotada.')
    if result.response_metadata.get('story_probable_identity'):
        # A storefront alternative must not replace the grounded visual hypothesis.
        reply = result.reply_text + '\n\n' + prefix
    else:
        reply = prefix if show_options and evidence['complete'] and evidence['products'] else prefix + '\n\n' + result.reply_text
    result.reply_text = reply
    result.response_metadata.update(story_clarification_reply=reply, ready_delivery_check=evidence,
                                    catalog_source=SOURCE)
    return result
