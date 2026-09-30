"""Keep import/payment support and document screenshots out of watch discovery."""
from __future__ import annotations

import re

from app.catalog.retrieval.text import fold_text
from app.models import AgentResult


def _import_topic(text: str) -> bool:
    return bool(re.search(r"\b(?:alfandega|aduaneir\w*|tributa\w*|imposto\w*|minhas importacoes)\b", fold_text(text)))


def _published_tax_policy() -> bool:
    from app.persona.persona_runtime import get_persona_runtime
    runtime = get_persona_runtime()
    if not runtime or not runtime.loaded or not runtime.enabled or runtime.load_error or not runtime.active_persona:
        return False
    if (runtime.active_persona.status != "active" or not runtime.workspace_id
            or runtime.active_persona.workspace_id != runtime.workspace_id):
        return False
    text = fold_text(runtime.active_persona.instructions or "")
    if re.search(r"(?:nao|nunca)[^.\n]{0,25}assume[^.\n]{0,80}impostos|impostos[^.\n]{0,30}nao[^.\n]{0,20}(?:inclusos|incluidos)|cliente\s+(?:paga|e responsavel)[^.\n]{0,30}impostos", text):
        return False
    return bool(re.search(
        r"assume[^.\n]{0,100}impostos|impostos[^.\n]{0,70}(?:inclusos|incluidos)|"
        r"nunca paga taxas extras[^.\n]{0,80}(?:alfandega|importacao)", text))


def import_support_reply(*, state=None, document: bool = False) -> AgentResult:
    policy = _published_tax_policy()
    parts = ["Recebi o print de acompanhamento da importação." if document else "Entendi a situação da importação."]
    if policy:
        parts.append("Nas compras da nossa loja, os impostos de importação já estão incluídos no valor da compra, conforme nossa política. A equipe precisa verificar essa cobrança específica.")
    else:
        parts.append("Preciso confirmar com a equipe a responsabilidade por essa cobrança no seu pedido.")
    if state and (getattr(state, "order_id", None) or getattr(state, "order_lookup_id", None)):
        parts.append("Quer que eu encaminhe esse caso para um atendente verificar?")
    else:
        parts.append("Qual é o número do pedido feito na loja, para verificarmos essa ocorrência?")
    text = "\n\n".join(parts)
    return AgentResult(reply_text=text, intent="support", response_metadata={
        "domain": "support", "response_source": "import_support",
        "support_document": document, "published_tax_policy": policy,
        "factual_fallback_text": text,
    })


def try_import_support(message, recent_turns, state=None):
    from app.ops.handoff_consent import customer_requests_human
    if customer_requests_human(message.text) or message.image_url:
        return None
    text = fold_text(message.text or "")
    prior = [str(t.get("content") or "") for t in (recent_turns or [])[-6:] if t.get("role") == "user"]
    followup = bool(re.search(r"\b(?:sou eu|quem paga|pagar isso|essa taxa|esse imposto)\b", text))
    if not _import_topic(text) and not (followup and any(_import_topic(t) for t in prior)):
        return None
    return import_support_reply(state=state)


def support_document_reply(identified):
    if identified.is_watch:
        return None
    # OCR is evidence, never instructions or authority to pay/follow links.
    text = " ".join(identified.visible_text or [])
    if _import_topic(text):
        return import_support_reply(document=True)
    if re.search(r"\b(?:rastreio|rastreamento|correios|comprovante|nota fiscal|pagamento)\b", fold_text(text)):
        return AgentResult(
            reply_text="Recebi o documento de acompanhamento do seu atendimento. Qual é o número do pedido e o que você precisa verificar?",
            intent="support", response_metadata={"domain": "support", "support_document": True,
                                                  "response_source": "support_document"})
    return None
