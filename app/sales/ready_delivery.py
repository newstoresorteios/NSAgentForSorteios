"""Deterministic ready-delivery lookup; never mix IDs from the two stores."""
from urllib.parse import urlparse

from app.models import AgentResult
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.sales.ready_delivery_context import resolve_query, scoped_context

SOURCE = 'https://www.newstorerj.com/pronta-entrega'
LISTING_CAVEAT = 'O estoque final e a entrega no prazo precisam ser confirmados antes de fechar.'


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


def contextual_listing_query(message, query, interpretation):
    """Keep the customer's occasion/deadline out of the storefront's AND search.

    Only replace prose for a semantically understood contextual request. Other
    requests keep the existing exact terms. Constraints this source cannot verify
    return to normal retrieval instead of silently broadening the search.
    """
    if interpretation is None or interpretation.domain != 'commerce':
        return query
    preferences = interpretation.preferences
    # Do not silently drop technical/product constraints that this public
    # name-only endpoint cannot express. The normal retrieval retains them.
    if any(value not in (None, '') for value in (
        preferences.budget_min, preferences.budget_max, preferences.material,
        preferences.mechanism, preferences.crystal, preferences.style,
    )):
        return None
    import re
    from app.sales.ready_delivery_context import SIZE
    attributes = [value for value in preferences.attributes
                  if value != 'ready_to_ship' and not value.startswith('qual:')]
    if any(not re.fullmatch(SIZE, value) for value in attributes):
        return None
    if not (preferences.occasion or preferences.delivery_deadline_text):
        return query
    # resolve_query already selected/refined a scoped earlier product identity.
    if query != message.text:
        return query
    subject = interpretation.subject
    from app.catalog.retrieval.tokens import extract_reference_code, preference_color_tokens
    terms = [subject.brand, subject.model, extract_reference_code(query) or subject.reference,
             subject.ean, *preference_color_tokens(interpretation)]
    terms.extend(re.findall(SIZE, query, re.I))
    terms.extend(attributes)
    # A generic request has no product identity; use the complete category.
    return ' '.join(dict.fromkeys(str(term).strip() for term in terms if term))[:500] or 'pronta entrega'


def validate_listing_reply(content, result):
    """Keep public evidence separate from transactional/product-sheet claims."""
    import html
    import re
    from app.catalog.retrieval.text import fold_text
    from app.llm.agent_contracts import AgentDecision, RiskAssessment
    from app.verify.factual_validator import validate_factual_response
    from app.sales.responder import recommendation_identifies_candidate

    evidence = public_listing_evidence(result)
    if not evidence:
        return None
    content = html.unescape(content.strip())
    # No generic "found options" response and no lost actionable links.
    if any(not recommendation_identifies_candidate(content, [
               {key: value for key, value in product.items() if key in {'name', 'reference'}}])
           or product['url'] not in content for product in evidence['products']):
        return None
    positions = [content.index(product['url']) for product in evidence['products']]
    if positions != sorted(positions):
        return None
    from app.ops.handoff_consent import promises_handoff
    if promises_handoff(content):
        return None
    folded = fold_text(content)
    # This source contains no fulfillment commitment, price, stock count or
    # promotion. Do not let a stylistic rewrite upgrade public listing evidence.
    if re.search(r'\b(?:em estoque|estoque confirmado|entrega garantida|envio imediato|'
                 r'chega(?:ra|m)?|recebera|garantimos|reservad[oa]|'
                 r'enviamos|entregamos|postamos|reais|parcelas|vezes sem juros)\b', folded):
        return None
    if SOURCE not in content:
        content = content.rstrip() + '\nCatálogo completo: ' + SOURCE
    if LISTING_CAVEAT not in content:
        content = content.rstrip() + '\n' + LISTING_CAVEAT
    candidate = result.model_copy(deep=True)
    candidate.reply_text = content
    report = validate_factual_response(candidate, mode='enforce', decision=AgentDecision(
        domain='commerce', risk=RiskAssessment(required_validations=['catalog_facts'])))
    return content if report.valid else None


async def try_ready_delivery(message, state=None, *, interpretation=None, recent_turns=None):
    if interpretation is not None and (
        interpretation.domain != 'commerce' or interpretation.goal in {'buy', 'compare', 'after_sales'}
        or any((interpretation.purchase_action, interpretation.checkout_action,
                interpretation.shipping_action, interpretation.order_action,
                interpretation.payment_action, interpretation.image_request))
    ):
        return None
    query = resolve_query(message, state)
    if not query or not enabled():
        return None
    query = contextual_listing_query(message, query, interpretation)
    if not query:
        return None
    evidence = await lookup(query)
    metadata = {'domain': 'commerce', 'response_source': 'ready_delivery_storefront',
                'catalog_source': SOURCE, 'ready_delivery_check': evidence,
                'ready_delivery_context': scoped_context(message, query),
                'clear_active_product': True, 'clear_presented_products': True}
    if not evidence['complete']:
        reply = 'Não consegui consultar nossa lista de pronta entrega agora. Isso não significa que o relógio esteja esgotado. Posso encaminhar para o comercial verificar?'
    elif evidence['requires_model']:
        reply = 'Você pode ver o catálogo de pronta entrega aqui: ' + SOURCE + '\nTem algum modelo em mente?'
    elif evidence['products']:
        from app.sales.consultative_response import sales_conversation_brief
        brief = sales_conversation_brief(interpretation, state)
        occasion = brief['known_preferences'].get('occasion')
        opening = ('Pensando em ' + ' '.join(str(occasion).split())[:120]
                   + ', vamos começar pelas opções de pronta entrega.\n\n') if occasion else ''
        reply = (opening + listing(evidence) + '\nCatálogo completo: ' + SOURCE
                 + '\n' + LISTING_CAVEAT)
    else:
        reply = ('A consulta não trouxe opções confirmadas agora.' if query == 'pronta entrega' else
                 'Não encontrei essa combinação de modelo e cor disponível na nossa lista atual de pronta entrega.')
        reply += '\nCatálogo completo: ' + SOURCE + '\nPosso encaminhar para o comercial verificar para você?'
    if not evidence['requires_model'] and (not evidence['complete'] or not evidence['products']):
        metadata['handoff'] = {'offer': True, 'required': False}
    result = AgentResult(reply_text=reply, intent='sales', response_metadata=metadata)
    if (evidence['complete'] and evidence['products'] and not evidence['requires_model']
            and interpretation is not None):
        from app.sales.responder import sales_response_with_openai
        generated = await sales_response_with_openai(
            message, {'intent': 'product_search', 'goal': interpretation.goal or 'find'},
            result, interpretation, state=state, recent_turns=recent_turns)
        if generated is not None:
            return generated
    return result


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
