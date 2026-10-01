"""Short-lived, tenant/contact-scoped storefront context (no foreign SKU IDs)."""
from datetime import datetime, timezone
import re

from app.catalog.retrieval.text import fold_text
from app.persona.persona_runtime import get_persona_runtime

EXPLICIT = r"(?:\bpronta[\s-]+entrega\b|\b(?:em|no) estoque\b)"
BLOCKED = r"\b(?:pedido|rastre\w*|despach\w*|imposto\w*|atendente|humano|cancel\w*|reembolso|troca|devolu\w*)\b|ja comprei"
COLORS = r"\b(?:azul|preto|prata|prateado|branco|verde|marrom|dourado|rosa|salmao)\b"
SIZE = r"\b\d{2}(?:[.,]\d)?\s*mm\b"
PRICE_FOLLOWUP = r"(?:e )?(?:qual (?:o )?(?:valor|preco)|quanto(?: custa)?|(?:manda|envia)(?: o)? link|(?:o )?(?:valor|preco|link))[.!?]*"
STOP = set("ola oi bom boa dia tarde noite tudo bem voces voce teria teriam tem esse essa esses essas este esta aquele aquela algum alguma relogio relogios modelo modelos na no de da do a o os as um uma cor pronta entrega disponivel disponibilidade por favor para gostaria saber se e em qual quanto custa valor preco ai hoje quero queria comprar preciso procuro procurando buscando busco encontrar consultar verificar pode podem poderia poderiam me informar sobre ha existe ainda obrigado obrigada estou com mostrador sim".split())
STOP.update({'consegue', 'passar', 'quais', 'que', 'estoque', 'ja', 'falei', 'esteja'})


def terms(text):
    return [v for v in re.findall(r"[a-z0-9]+", fold_text(text)) if v not in STOP and len(v) > 1]


def scoped_context(message, query):
    runtime = get_persona_runtime()
    if not runtime or not runtime.workspace_id or not message.conversation_id or not message.sender_key:
        return None
    return {"tenant_id": str(runtime.workspace_id), "conversation_id": message.conversation_id,
            "sender_key": message.sender_key, "channel": message.channel,
            "query": query[:500], "created_at": datetime.now(timezone.utc).isoformat()}


def valid_context(message, state):
    ctx = getattr(state, "ready_delivery_context", None)
    current = scoped_context(message, "")
    if not isinstance(ctx, dict) or not current:
        return None
    if any(ctx.get(k) != current[k] for k in ("tenant_id", "conversation_id", "sender_key", "channel")):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ctx["created_at"])).total_seconds()
    except (ValueError, TypeError, KeyError):
        return None
    return ctx if 0 <= age <= 86400 else None


def resolve_query(message, state=None):
    text = fold_text(message.text or "")
    if (message.image_url or message.instagram_story or re.search(BLOCKED, text)
            or re.search(r"\bnao\b.{0,25}" + EXPLICIT + r"|\bsob encomenda\b", text)):
        return None
    explicit = bool(re.search(EXPLICIT, text))
    ctx = valid_context(message, state)
    if explicit:
        if not terms(re.sub(EXPLICIT, '', text)):
            return ctx['query'] if ctx else 'pronta entrega'
        if terms(re.sub(EXPLICIT, "", text)) or not ctx:
            return message.text
        return ctx["query"]
    if not ctx or len(text) > 160 or re.search(r"https?://|\b(?:outro|outra|agora|obrigad\w*|tchau)\b", text):
        return None
    if re.search(r'casamento|final de semana|fim de semana|ja falei|dificil entender|\bpqp\b|para mim', text):
        return ctx['query']
    if re.fullmatch(PRICE_FOLLOWUP, text.strip()) and terms(ctx['query']):
        return ctx['query']
    if not terms(text):
        return None
    prior = ctx["query"]
    if not terms(prior):
        return text
    facets = re.sub(SIZE, "", re.sub(COLORS, "", text))
    new_terms = set(terms(facets))
    if new_terms and not new_terms.issubset(set(terms(prior))):
        return None
    if re.search(SIZE, text):
        prior = re.sub(SIZE, "", prior)
    if re.search(COLORS, text):
        prior = re.sub(COLORS, "", prior)
    return (prior + " " + text).strip()[:500]
