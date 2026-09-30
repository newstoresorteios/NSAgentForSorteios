"""Deterministic ready-delivery lookup; never mix IDs from the two stores."""
import re
from urllib.parse import urlparse

from app.models import AgentResult
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError


async def try_ready_delivery(message):
    text = message.text or ''
    if not re.search(r'pronta[\s-]+entrega', text, re.I):
        return None
    # Delivery of an existing order belongs to post-sales.
    if re.search(r'meu pedido|rastre|despach|ja comprei|já comprei', text, re.I):
        return None
    from app.persona.site_knowledge import STORE_PRONTA_ENTREGA_URL
    configured = str(STORE_PRONTA_ENTREGA_URL() or '')
    if urlparse(configured).hostname not in {'www.newstorerj.com', 'newstorerj.com'}:
        return None
    metadata = {'response_source': 'ready_delivery_storefront', 'catalog_source': 'https://www.newstorerj.com/pronta-entrega'}
    try:
        data = await TrayAdapterClient().search_ready_delivery(text)
    except TrayAdapterError:
        data = {}
    if not data.get('success') or not data.get('complete'):
        reply = 'Não consegui consultar nossa lista de pronta entrega agora. Isso não significa que o relógio esteja esgotado. Posso encaminhar para o comercial verificar?'
    elif data.get('requiresModel'):
        reply = 'Qual modelo, referência e cor você procura à pronta entrega? Vou consultar a lista da nossa loja: https://www.newstorerj.com/pronta-entrega'
    else:
        products = data.get('products') or []
        available = [p for p in products if p.get('listedAvailable') and urlparse(str(p.get('url') or '')).hostname == 'www.newstorerj.com']
        metadata['checked_at'] = data.get('checkedAt')
        if available:
            reply = 'Encontrei estas opções anunciadas como disponíveis na nossa lista de pronta entrega:\n' + '\n'.join(f"• {p['name']}\n{p['url']}" for p in available[:3])
            reply += '\nO estoque final precisa ser confirmado antes de fechar. Quer que o comercial confirme a peça e te ajude com a compra?'
        else:
            reply = 'Não encontrei essa combinação de modelo e cor disponível na nossa lista atual de pronta entrega. Posso encaminhar para o comercial verificar essa peça para você?'
    if not data.get('requiresModel'):
        metadata['handoff'] = {'offer': True, 'required': False}
    return AgentResult(reply_text=reply, intent='sales', response_metadata=metadata)
