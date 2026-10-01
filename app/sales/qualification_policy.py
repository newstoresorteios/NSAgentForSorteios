"""One qualification budget shared by fixed and catalog-guided discovery."""
import re

from app.catalog.retrieval.text import _fold


def qualification_policy(config):
    from app.persona.persona_runtime import get_persona_runtime
    from app.sales.discovery import _max_qualification_questions

    runtime = get_persona_runtime()
    enabled = not (runtime is not None and runtime.loaded and not runtime.load_error
                   and (not runtime.enabled or not runtime.require_qualification_before_catalog))
    return enabled, min(config['maxQuestions'], _max_qualification_questions())


def requests_catalog(text, *patterns):
    normalized = _fold(text or '')
    inventory = bool(re.search(r'\b(?:quais|mostr\w*|liste|listar)\b', normalized)
                     and re.search(r'\b(?:estoque|pronta entrega)\b', normalized))
    return inventory or any(re.search(pattern, text or '', re.I) for pattern in patterns if pattern)


def question_history(turns, topic):
    from .contextual_discovery import topic_key
    asked, count = set(), 0
    for turn in reversed(turns or []):
        if turn.get('role') != 'assistant':
            continue
        metadata = turn.get('metadata') or {}
        if not isinstance(metadata, dict):
            continue
        metadata = metadata.get('response_metadata', metadata)
        if not isinstance(metadata, dict):
            continue
        if metadata.get('dialogue_phase_reset') or metadata.get('presented_products'):
            break
        marker = metadata.get('discovery_question') or {}
        if isinstance(marker, dict) and marker:
            if topic_key(marker.get('topic')) != topic:
                break
            count += 1
            asked.add(marker.get('slot'))
        # Errors, acknowledgements and handoff offers don't erase questions.
    return asked, count
