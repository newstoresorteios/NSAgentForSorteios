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


async def enrich_story_ready_delivery(message, result):
    """Search the second storefront without upgrading a visual guess to a match."""
    ref = result.response_metadata.get('last_story_product')
    scope = scoped_context(message, '')
    if (not isinstance(ref, dict) or ref.get('match_status') not in {'ambiguous', 'not_found'}
            or not ref.get('catalog_query') or not enabled() or not scope
            or any(ref.get(k) != scope[k] for k in ('tenant_id', 'conversation_id', 'sender_key'))):
        return result
    evidence = await lookup(ref['catalog_query'])
    selected = ref.get('selected_option')
    if not evidence['complete']:
        prefix = 'Não consegui consultar a lista de pronta entrega agora; isso não significa que a peça esteja esgotada.'
    elif selected and evidence['products']:
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
    reply = prefix if selected and evidence['complete'] and evidence['products'] else prefix + '\n\n' + result.reply_text
    result.reply_text = reply
    result.response_metadata.update(story_clarification_reply=reply, ready_delivery_check=evidence,
                                    catalog_source=SOURCE)
    return result
