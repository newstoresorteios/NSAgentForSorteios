"""Read-only institutional questions, distinct from after-sales actions."""
from __future__ import annotations

import hashlib
import json
import re

from app.configuration.runtime import current_bundle, message, policy
from app.catalog.retrieval.text import fold_text
from app.persona.store_knowledge import fetch_institutional_knowledge


def institutional_question(text):
    values = current_bundle().get('values', {})
    if not values.get('institutionalDirectAnswerEnabled', False):
        return False
    rules = json.loads(policy('institutionalRoutingRules'))
    normalized = fold_text(text or '')
    return bool(re.search(rules['question'], normalized)
                and not re.search(rules['action'], normalized))


async def answer_institutional(incoming):
    if not institutional_question(incoming.text):
        return None
    documents = fetch_institutional_knowledge(incoming.text).items
    if not documents:
        return None
    from app.config import get_settings
    from app.models import AgentResult
    from app.llm.openai_gateway import generate_text_output
    from app.llm.prompt_compiler import resolve_system_instructions
    from app.ops.observability import log_event
    settings = get_settings()
    sources = '\n'.join(dict.fromkeys(d['source_url'] for d in documents if d.get('source_url')))
    reply = message('institutional_answer_fallback', sources=sources)
    used_model = False
    if settings.openai_api_key:
        try:
            generated = await generate_text_output(
                model=settings.openai_model, call_type='response_composition',
                messages=[{'role':'system','content':resolve_system_instructions(
                    fallback_instructions=message('institutional_answer_instructions'),
                    incoming=incoming, relevant_knowledge=documents,
                    extra_system_blocks=[message('institutional_answer_instructions')])},
                    {'role':'user','content':incoming.text}])
            if generated.text.strip():
                reply, used_model = generated.text.strip(), True
        except Exception as exc:
            log_event('institutional.composition_failed', {'error_type':type(exc).__name__})
    evidence = [{k:d[k] for k in ('slug','title','source_url','reviewed_at') if k in d}
                | {'content_hash':hashlib.sha256(d['body'].encode()).hexdigest()} for d in documents]
    log_event('institutional.retrieved', {'documents':evidence, 'used_model':used_model})
    return AgentResult(reply_text=reply, intent='general',
        commercial_data={'institutional_documents':documents},
        response_metadata={'domain':'institutional', 'institutional_evidence':evidence,
            'response_source':'institutional_knowledge', 'used_openai_responder':used_model,
            'used_openai_interpreter':False, 'used_tray':False})
