"""Operator-managed discovery, bounded to the current catalog conversation."""
from __future__ import annotations

import json
import re
from typing import Any

from app.configuration.runtime import current_bundle


def configuration() -> dict[str, Any]:
    values = current_bundle().get("values") or {}
    if values.get("contextualDiscoveryEnabled") is not True:
        return {}
    try:
        config = json.loads(values.get("contextualDiscoveryRules") or "{}")
        if not isinstance(config, dict) or not isinstance(config.get("questions"), list):
            return {}
        if type(config["maxQuestions"]) is not int or not 1 <= config["maxQuestions"] <= 6:
            return {}
        if type(config.get("detailLimit")) is not int or not 0 <= config["detailLimit"] <= 6:
            return {}
        if not isinstance(config.get("readyGroups"), list) or any(
            not isinstance(group, list) or any(not isinstance(slot, str) for slot in group)
            for group in config["readyGroups"]
        ):
            return {}
        for question in config["questions"]:
            if not all(isinstance(question.get(k), str) and question[k] for k in ("slot", "instruction", "fallback")):
                return {}
        re.compile(config["directRequestPattern"], re.I)
        return config
    except (ValueError, TypeError, KeyError, re.error, AttributeError):
        # Invalid operator edits preserve the existing discovery behavior.
        return {}


def grounded_no_preferences(claimed, asked, recent_turns, message_text):
    """Do not turn 'no model in mind' into indifference to every product facet."""
    from app.catalog.retrieval.text import _fold
    utterances = [str(t.get('content') or '') for t in recent_turns or [] if t.get('role') == 'user']
    utterances.append(message_text or '')
    aliases = {'brand': r'marca', 'color': r'cor|cores|mostrador', 'style': r'estilo',
               'material': r'material|pulseira', 'strap': r'pulseira', 'occasion': r'ocasiao|uso',
               'case_size': r'tamanho|caixa', 'budget': r'orcamento|preco|valor', 'recipient': r'presente|destinatario'}
    supported = set(asked)
    for utterance in utterances:
        text = _fold(utterance)
        if re.search(r'\b(?:sem (?:nenhuma|qualquer) preferencia|tanto faz tudo|qualquer relogio serve)\b', text):
            return set(claimed)
        for clause in re.split(r'[.!?;]|\bmas\b', text):
            if not re.search(r'\b(?:sem preferencia|indiferente|tanto faz|qualquer)\b', clause):
                continue
            supported.update(slot for slot, pattern in aliases.items() if re.search(r'\b(?:' + pattern + r')\b', clause))
    return set(claimed) & supported


def apply_contextual_discovery(interpretation, state, recent_turns, message_text, *, fallback=False):
    if interpretation._adaptive_ready:
        state.update(persona_qualification_required=False, force_retrieval=True,
                     contextual_discovery_reason="ready")
        return
    from .adaptive_discovery import configuration as adaptive_configuration
    if adaptive_configuration() and not fallback:
        # The async catalog-guided gate replaces this fixed-question gate.
        return
    config = configuration()
    if not config or interpretation.domain != "commerce":
        return
    from app.catalog.specs.identity_lock import specific_product_lock
    from app.sales.dialogue_phase import message_resets_dialogue_to_discovery
    new_browse = message_resets_dialogue_to_discovery(message_text, interpretation)

    # Existing purchase and order routes retain authority over discovery.
    if (specific_product_lock(interpretation) or (state.get("has_bound_sale_target") and not new_browse)
        or interpretation.goal in {"buy", "inspect", "after_sales", "compare"}
        or interpretation.purchase_action or interpretation.shipping_action
        or interpretation.checkout_action or interpretation.order_action
        or interpretation._variant_refinement or interpretation.image_request):
        return
    if not (interpretation.subject.brand or interpretation.subject.product_type):
        return

    topic = (interpretation.subject.brand or interpretation.subject.product_type or "").casefold()
    asked = set()
    has_markers = False
    # Stop at a completed catalog answer; a later purchase starts fresh.
    for turn in reversed(recent_turns or []):
        if turn.get("role") != "assistant":
            continue
        metadata = turn.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        metadata = metadata.get("response_metadata", metadata)
        if not isinstance(metadata, dict):
            continue
        marker = metadata.get("discovery_question") or {}
        if not isinstance(marker, dict):
            marker = {}
        if marker:
            has_markers = True
            if marker.get("topic") != topic:
                break
            asked.add(marker.get("slot"))
        elif metadata.get("safety_reason") != "commerce_clarification":
            break

    known = dict(state.get("known_preferences") or {})
    # Customer identity is not a product preference or a reason to skip discovery.
    if not any(not str(a).startswith("qual:") for a in interpretation.preferences.attributes):
        known.pop("attributes", None)
    if any(str(a).startswith("required_strap_material:") for a in interpretation.preferences.attributes):
        known["strap"] = True
    if interpretation.subject.model:
        known["model_intent"] = True
    covered = set(known) | grounded_no_preferences(
        interpretation.preferences.explicit_no_preferences, asked, recent_turns, message_text)
    ready_groups = config.get("readyGroups") or []
    ready = any(set(group) <= covered for group in ready_groups if isinstance(group, list) and group)
    direct = bool(re.search(config["directRequestPattern"], message_text or "", re.I))
    count = len(asked) if has_markers else int(state.get("clarification_count") or 0)
    if ready or direct or interpretation.stop_clarification or count >= config["maxQuestions"]:
        state.update(persona_qualification_required=False, force_retrieval=True,
                     contextual_discovery_reason="ready" if ready else "limit_or_request")
        return
    for question in config["questions"]:
        slot = question["slot"]
        if slot in covered or slot in asked:
            continue
        state.update(persona_qualification_required=True, force_retrieval=False,
                     contextual_question={**question, "topic": topic},
                     contextual_discovery_reason="missing_preference")
        return
    state.update(persona_qualification_required=False, force_retrieval=True,
                 contextual_discovery_reason="questions_exhausted")
