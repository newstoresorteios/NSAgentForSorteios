"""Retain unresolved visual context without silently choosing another SKU."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from app.catalog.retrieval.text import fold_text
from app.catalog.retrieval.tokens import extract_reference_code
from app.models import AgentResult
from app.persona.persona_runtime import get_persona_runtime


def unresolved_story_followup(incoming, state):
    if incoming.channel != "instagram" or incoming.instagram_story or incoming.image_url:
        return None
    ref = getattr(state, "last_story_product", None)
    if not isinstance(ref, dict) or ref.get("match_status") not in {"ambiguous", "not_found"}:
        return None
    runtime = get_persona_runtime()
    if not runtime or not runtime.workspace_id or str(runtime.workspace_id) != ref.get("tenant_id"):
        return None
    if (not incoming.conversation_id or incoming.conversation_id != ref.get("conversation_id")
            or incoming.sender_key != ref.get("sender_key")):
        return None
    try:
        captured = datetime.fromisoformat(str(ref.get("resolved_at")))
        age = (datetime.now(timezone.utc) - captured).total_seconds()
    except (ValueError, TypeError):
        return None
    if not 0 <= age <= 86400:
        return None
    text = fold_text(incoming.text or "")
    # A new subject, reference, link or photo must remain free to be searched.
    if (len(text) > 160 or extract_reference_code(incoming.text)
            or re.search(r"https?://|\b(?:outro|outra|pedido|rastreio|imposto|atendente|humano)\b", text)):
        return None
    selection = re.search(r"\b(?:azul|preto|prata|prateado|branco|verde|marrom|dourado|rosa|esquerda|direita|centro|primeiro|segundo|terceiro)\b", text)
    terms = set(re.findall(r"[a-z0-9]+", text))
    related = bool(terms.intersection(ref.get("followup_terms") or []))
    selection_words = set("o a os as um uma eu quero esse este aquele de do da no na com por favor mostrador relogio azul preto prata prateado branco verde marrom dourado rosa esquerda direita centro primeiro segundo terceiro quanto custa valor preco link manda envia".split())
    selection_only = bool(selection and terms.issubset(selection_words))
    if not selection_only and not related and not re.fullmatch(r"\s*\d{2}\s*mm[.!?]?\s*", text):
        return None
    options = [v for v in ref.get("clarification_options", []) if str(v).startswith("relógio ")][:5]
    rounds = int(ref.get("clarification_rounds") or 0)
    if not selection and options and rounds == 0:
        reply = "Para consultar a versão certa do Story, qual você quer: " + ", ".join(options[:-1])
        reply += (" ou " if len(options) > 1 else "") + options[-1] + "?"
    else:
        reply = ("Ainda não confirmei a referência exata do relógio desse Story. "
                 "Pode enviar um print marcando o relógio ou a referência? "
                 "Também posso pedir ajuda a um atendente para confirmar a versão certa.")
    updated = {**ref, "clarification_rounds": min(rounds + 1, 2)}
    return AgentResult(reply_text=reply, intent="commerce", safety_reason="ambiguous",
                       response_metadata={"domain": "commerce", "response_source": "instagram_story_followup",
                                          "instagram_story": True, "story_match_status": "ambiguous",
                                          "story_clarification_reply": reply, "last_story_product": updated,
                                          "clear_active_product": True, "clear_presented_products": True,
                                          "clear_pending_action": True, "product_resolution_state": "unresolved"})
