"""Catalog-guided discovery. Cached candidates are evidence, never an offer."""
from __future__ import annotations

import json
import re
import time
from copy import deepcopy

from app.configuration.runtime import current_bundle, message as operator_message
from app.models import AgentResult, ProductPreferences
from app.catalog.retrieval.text import _fold
from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.specs.catalog_specs import extract_case_size_mm, interpretation_case_size_range
from app.sales.contextual_discovery import configuration as contextual_configuration


def configuration():
    values = current_bundle().get("values") or {}
    if values.get("adaptiveDiscoveryEnabled") is not True:
        return {}
    try:
        rules = json.loads(values["adaptiveDiscoveryRules"])
        for key, low, high in (("reviewAfterQuestions", 1, 10), ("maxSearches", 1, 10),
                               ("candidateLimit", 2, 50), ("cacheTtlSeconds", 1, 3600)):
            if type(rules[key]) is not int or not low <= rules[key] <= high:
                return {}
        if not isinstance(rules["facets"], list):
            return {}
        if not isinstance(rules.get("criteriaLabels"), dict) or not all(
            isinstance(v, str) for v in rules["criteriaLabels"].values()
        ):
            return {}
        for facet in rules["facets"]:
            if facet["slot"] not in {"case_size", "color", "strap", "budget", "occasion"}:
                return {}
            if not isinstance(facet["fields"], list) or not all(isinstance(f, str) for f in facet["fields"]):
                return {}
        re.compile(rules["showResultsPattern"], re.I)
        return rules
    except (ValueError, TypeError, KeyError, AttributeError, re.error):
        return {}


def _previous(recent_turns, topic):
    for turn in reversed(recent_turns or []):
        if turn.get("role") != "assistant":
            continue
        metadata = turn.get("metadata") or {}
        if not isinstance(metadata, dict):
            break
        marker = metadata.get("discovery_question") or {}
        if not isinstance(marker, dict):
            break
        snapshot = marker.get("adaptive") or {}
        if snapshot and marker.get("topic") == topic:
            return deepcopy(snapshot), marker.get("slot")
        # A completed response or changed topic starts another discovery.
        break
    return {}, None


def _eligible(interpretation, state, text):
    from app.catalog.specs.identity_lock import specific_product_lock
    from app.sales.dialogue_phase import message_resets_dialogue_to_discovery
    from app.sales.qualification_slots import has_bound_sale_target
    if interpretation is None or interpretation.domain != "commerce":
        return False
    if interpretation.resolved_answer_strategy() in {"handoff", "refuse", "acknowledge"}:
        return False
    if (specific_product_lock(interpretation) or interpretation.image_request
        or interpretation._variant_refinement or interpretation.goal in {"buy", "inspect", "after_sales", "compare"}
        or any((interpretation.purchase_action, interpretation.checkout_action,
                interpretation.shipping_action, interpretation.order_action, interpretation.payment_action))):
        return False
    if has_bound_sale_target(state) and not message_resets_dialogue_to_discovery(text, interpretation):
        return False
    prefs = interpretation.preferences
    if not interpretation.subject.brand and (
        interpretation.ready_for_retrieval or prefs.budget_max is not None
        or not any((prefs.color, prefs.style, prefs.occasion,
                    [a for a in prefs.attributes if not str(a).startswith("qual:")], prefs.material))
    ):
        # Preserve established category/budget searches and avoid querying the
        # entire store before a completely unspecified consultative request.
        return False
    return bool(interpretation.subject.brand or interpretation.subject.product_type)


def _restore_preferences(interpretation, snapshot, slot, text):
    prior = snapshot.get("preferences") or {}
    current = interpretation.preferences.model_dump()
    no_preference = set(current.get("explicit_no_preferences") or [])
    for key, value in prior.items():
        if key not in no_preference and not current.get(key) and value:
            current[key] = value
    # Keep explicit current attributes authoritative for their dimension.
    attrs = list(current.get("attributes") or [])
    prefixes = {a.split(":", 1)[0] for a in attrs if ":" in a}
    if "attributes" not in no_preference:
        attrs.extend(a for a in prior.get("attributes", []) if a not in attrs and a.split(":", 1)[0] not in prefixes
                     and not ("material" in no_preference and a.startswith("required_strap_material:")))
    current["attributes"] = attrs
    if "case_size" in no_preference:
        current["attributes"] = [a for a in current["attributes"] if not a.startswith("case_size:")]
    if no_preference & {"material", "strap"}:
        current["attributes"] = [a for a in current["attributes"] if not a.startswith("required_strap_material:")]
    interpretation.preferences = ProductPreferences(**current)
    from app.catalog.specs.preference_normalize import extract_bare_budget_amount, extract_stated_strap_material
    prefs = interpretation.preferences
    if slot == "budget":
        amount = extract_bare_budget_amount(text)
        if amount is not None:
            prefs.budget_max = amount
    elif slot == "strap":
        material = extract_stated_strap_material(text)
        if material:
            prefs.attributes = [a for a in prefs.attributes if not a.startswith("required_strap_material:")]
            prefs.attributes.append("required_strap_material:" + material)
    elif slot == "case_size":
        from app.catalog.specs.catalog_specs import extract_case_size_range_from_text
        size = extract_case_size_range_from_text(text)
        if size is None and re.fullmatch(r"\d{2}", text.strip()):
            if text.strip() + " mm" in (snapshot.get("last_options") or []):
                size = (int(text.strip()), int(text.strip()))
        if size:
            prefs.attributes = [a for a in prefs.attributes if not a.startswith("case_size:")]
            prefs.attributes.append(f"case_size:{size[0]}-{size[1]}mm")
    elif slot in {"color", "occasion"}:
        # Only exact catalog values are safe to bind deterministically.
        choices = snapshot.get("last_options") or []
        choice = next((v for v in choices if _fold(v) == _fold(text)), None)
        if choice:
            setattr(prefs, slot, choice)


def _compact(product):
    keys = ("id", "name", "brand", "model", "reference", "case_size", "case_size_mm",
            "dial_color", "color", "strap_type", "strap_material", "bracelet_material", "material",
            "style", "occasion", "mechanism", "crystal", "current_price", "price", "promotional_price",
            "available", "available_in_store", "stock", "water_resistance_m", "description")
    return {k: (v[:1500] if isinstance(v, str) else v) for k, v in product.items()
            if k in keys and isinstance(v, (str, int, float, bool, type(None)))}


def _facet_values(product, facet):
    if facet["slot"] == "case_size":
        size = extract_case_size_mm(product)
        return [size + " mm"] if size else []
    values = []
    for key in facet["fields"]:
        value = product.get(key)
        if value is not None and str(value).strip():
            values.append(str(value).strip())
            break
    return values


def _query(interpretation, limit):
    from app.sales.tray_refresh import tray_list_query_extras
    args = {**tray_list_query_extras(interpretation), "limit": limit, "page": 1}
    if interpretation.subject.brand:
        args["brand"] = interpretation.subject.brand
    else:
        args["name"] = interpretation.subject.product_type
    return args


def _can_reuse(snapshot, args, rules, now):
    if not snapshot.get("candidates") or now - snapshot.get("fetched_at", 0) > rules["cacheTtlSeconds"]:
        return False
    scope = snapshot.get("query") or {}
    # Reuse only an equal or broader query. Widening any filter requires a refresh.
    for key, old in scope.items():
        if key in {"limit", "page"}:
            continue
        new = args.get(key)
        if key == "current_price_range" and new and old:
            if float(new.split(",")[1]) <= float(old.split(",")[1]):
                continue
        if old != new:
            return False
    return True


def _blocked(interpretation, reason, snapshot):
    from app.sales.result_utils import mark_sales_result
    return mark_sales_result(AgentResult(
        reply_text=operator_message("adaptive_discovery_" + reason), intent="commerce",
        safety_reason="adaptive_discovery_" + reason,
        response_metadata={"adaptive_discovery": {"decision": reason, "searches": snapshot.get("searches", 0)}}),
        interpretation=interpretation, goal=interpretation.goal, response_source="published_discovery_policy",
        used_openai_responder=False, used_tray=True)


def unconfirmed_result(interpretation):
    """Never silently relax a requirement to fill a recommendation."""
    rules = configuration()
    labels = rules.get("criteriaLabels") or {}
    prefs = interpretation.preferences
    values = {"brand": interpretation.subject.brand, "color": prefs.color,
              "budget": prefs.budget_max, "mechanism": prefs.mechanism, "crystal": prefs.crystal}
    size = interpretation_case_size_range(interpretation)
    if size:
        values["case_size"] = f"{size[0]}–{size[1]} mm"
    for attribute in prefs.attributes:
        if attribute.startswith("required_strap_material:"):
            values["strap"] = attribute.split(":", 1)[1]
    criteria = "; ".join(f"{labels[key]}: {value}" for key, value in values.items()
                         if value is not None and key in labels)
    from app.sales.result_utils import mark_sales_result
    return mark_sales_result(AgentResult(
        reply_text=operator_message("adaptive_discovery_unconfirmed", criteria=criteria),
        intent="commerce", safety_reason="adaptive_discovery_unconfirmed",
        response_metadata={"presented_products": False, "adaptive_discovery": {"decision": "unconfirmed"}}),
        interpretation=interpretation, goal=interpretation.goal, response_source="published_discovery_policy",
        used_openai_responder=False, used_tray=True)


async def _ask_contextual_fallback(interpretation, state, message, recent_turns, generate_reply, *, used_tray):
    from .discovery import _discovery_state
    from .contextual_discovery import apply_contextual_discovery

    discovery = _discovery_state(interpretation, recent_turns, message_text=message.text,
                                 commerce_state=state)
    apply_contextual_discovery(interpretation, discovery, recent_turns, message.text, fallback=True)
    if not discovery.get("persona_qualification_required") or not discovery.get("contextual_question"):
        if discovery.get("contextual_discovery_reason") in {"ready", "limit_or_request", "questions_exhausted"}:
            interpretation._adaptive_ready = True
            interpretation._adaptive_trace = {"decision": "search", "reason": discovery["contextual_discovery_reason"]}
        return None
    interpretation._adaptive_trace = {"decision": "ask", "reason": "contextual_fallback",
                                       "slot": discovery["contextual_question"]["slot"]}
    return await generate_reply(message=message, interpretation=interpretation,
                                recent_turns=recent_turns, discovery_state=discovery, used_tray=used_tray)


async def prepare_discovery(*, interpretation, state, message, recent_turns, execute_tool, generate_reply):
    rules, contextual = configuration(), contextual_configuration()
    if not rules or not contextual or interpretation is None:
        return None
    from app.memory.history_window import turns_for_conversation
    recent_turns = turns_for_conversation(recent_turns, message.conversation_id)
    topic = _fold(interpretation.subject.brand or interpretation.subject.product_type)
    last_assistant = next((t for t in reversed(recent_turns or []) if t.get("role") == "assistant"), {})
    metadata = last_assistant.get("metadata") or {}
    previous_question = metadata.get("discovery_question") if isinstance(metadata, dict) else None
    previous_question = previous_question if isinstance(previous_question, dict) else {}
    if (previous_question and not previous_question.get("adaptive")
            and _fold(previous_question.get("topic")) == topic
            and not interpretation.domain_change_explicit):
        # A budget answer completes one slot, not the entire ongoing interview.
        question = await _ask_contextual_fallback(
            interpretation, state, message, recent_turns, generate_reply, used_tray=False)
        if question is not None or interpretation._adaptive_ready:
            return question
    if not _eligible(interpretation, state, message.text):
        prefs = interpretation.preferences
        # An unspecified browse needs a question, not a whole-store lookup.
        # Existing budget/category, exact-product and purchase routes are preserved.
        if (interpretation.domain == "commerce" and interpretation.goal == "discover"
                and not interpretation.subject.brand and prefs.budget_max is None
                and not any((prefs.color, prefs.style, prefs.occasion, prefs.material,
                             prefs.mechanism, prefs.crystal,
                             [a for a in prefs.attributes if not str(a).startswith("qual:")]))):
            return await _ask_contextual_fallback(
                interpretation, state, message, recent_turns, generate_reply, used_tray=False)
        return None
    snapshot, previous_slot = _previous(recent_turns, topic)
    if interpretation.domain_change_explicit or not interpretation.references_previous_context:
        snapshot, previous_slot = {}, None
    _restore_preferences(interpretation, snapshot, previous_slot, message.text)
    asked = list(snapshot.get("asked") or [])
    if previous_slot and previous_slot not in asked:
        asked.append(previous_slot)
    direct = interpretation.stop_clarification or any(
        re.search(pattern, message.text or "", re.I)
        for pattern in (contextual["directRequestPattern"], rules["showResultsPattern"])
    )
    if direct:
        interpretation._adaptive_ready = True
        interpretation._adaptive_trace = {"decision": "search", "reason": "explicit_request"}
        return None
    args = _query(interpretation, rules["candidateLimit"])
    now = time.time()
    reused = _can_reuse(snapshot, args, rules, now)
    if not reused:
        searches = int(snapshot.get("searches") or 0)
        if searches >= rules["maxSearches"]:
            return _blocked(interpretation, "limit", snapshot)
        snapshot["searches"] = searches + 1
        try:
            result = await execute_tool("search_products", args)
        except Exception:
            return _blocked(interpretation, "unavailable", snapshot)
        if not isinstance(result, dict) or result.get("error") or not isinstance(result.get("products"), list):
            return _blocked(interpretation, "unavailable", snapshot)
        snapshot.update(candidates=[_compact(p) for p in result["products"][:rules["candidateLimit"]]
                                   if isinstance(p, dict) and p.get("id") is not None],
                        query=args, fetched_at=now)
    candidates = snapshot.get("candidates") or []
    matches = hard_filter_products(candidates, interpretation, mode="recommendation", message_text=message.text)
    interpretation._adaptive_trace = {
        "decision": "search", "reason": "full_lookup",
        "candidate_count": len(candidates), "compatible_count": len(matches),
        "filtered_out": len(candidates) - len(matches), "cache_reused": reused,
        "preliminary_searches": snapshot.get("searches", 0), "asked_slots": asked,
        "review_due": len(asked) >= rules["reviewAfterQuestions"],
    }
    snapshot.update(asked=asked, preferences=interpretation.preferences.model_dump(mode="json"))
    if not matches:
        prefs = interpretation.preferences
        constrained = any((prefs.budget_max is not None, prefs.budget_min is not None,
                           prefs.color, prefs.style, prefs.occasion, prefs.material,
                           prefs.mechanism, prefs.crystal,
                           [a for a in prefs.attributes if not str(a).startswith("qual:")],
                           interpretation_case_size_range(interpretation, message_text=message.text)))
        if not constrained:
            question = await _ask_contextual_fallback(
                interpretation, state, message, recent_turns, generate_reply, used_tray=True)
            if question is not None:
                return question
        # A bounded preliminary pool cannot establish that the store has no match.
        # Continue with the existing full lookup and live evidence recovery.
        interpretation._adaptive_ready = True
        return None
    known = {k for k, v in interpretation.preferences.model_dump().items() if v}
    known |= set(interpretation.preferences.explicit_no_preferences)
    if interpretation_case_size_range(interpretation, message_text=message.text):
        known.add("case_size")
    if any(a.startswith("required_strap_material:") for a in interpretation.preferences.attributes):
        known.add("strap")
    if interpretation.preferences.budget_min is not None or interpretation.preferences.budget_max is not None:
        known.add("budget")
    questions = {q["slot"]: q for q in contextual["questions"]}
    choices = []
    for priority, facet in enumerate(rules["facets"]):
        slot = facet["slot"]
        if slot in known or slot in asked or slot not in questions:
            continue
        values = {v for p in matches for v in _facet_values(p, facet)}
        if len(values) > 1:
            choices.append((len(values), -priority, slot, sorted(values)))
    if len(matches) == 1 or not choices:
        interpretation._adaptive_ready = True
        return None
    _, _, slot, options = max(choices)
    interpretation._adaptive_trace.update(decision="ask", reason="catalog_difference", slot=slot)
    from app.ops.observability import log_event
    log_event("catalog.adaptive_discovery", interpretation._adaptive_trace)
    snapshot["last_options"] = options
    question = {**questions[slot], "topic": topic, "catalog_options": options,
                "adaptive": snapshot}
    discovery = {"persona_qualification_required": True, "contextual_question": question,
                 "known_preferences": snapshot["preferences"], "clarification_count": len(asked),
                 "review_due": len(asked) >= rules["reviewAfterQuestions"],
                 "candidate_count": len(matches), "force_retrieval": False}
    # Each question must separate actual candidates. Review never forces an offer.
    return await generate_reply(message=message, interpretation=interpretation,
                                recent_turns=recent_turns, discovery_state=discovery, used_tray=True)
