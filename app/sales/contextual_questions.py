"""Read-only product follow-ups must run before purchase/discovery shortcuts."""
from __future__ import annotations

import json
import re
from app.catalog.retrieval.text import fold_text
from app.configuration.runtime import message as copy, policy
from app.models import AgentResult, ProductPreferences


def try_name_answer(incoming, interpretation, state):
    """A name answer does not erase a known pending shopping topic."""
    if interpretation is None or interpretation.domain != 'commerce':
        return None
    from app.sales.qualification_slots import extract_introduced_name
    name = extract_introduced_name(incoming.text)
    topic = interpretation.active_topic or state.active_topic
    acknowledgement = (getattr(interpretation, '_slot_answer_hold', False)
                       or interpretation.answer_strategy == 'acknowledge'
                       or (name and len(incoming.text.split()) <= len(name.split()) + 3))
    if (not name or not topic or not acknowledgement
            or state.cart_session_id or state.order_id):
        return None
    from app.sales.result_utils import mark_sales_result
    template = ('named_customer_known_request' if interpretation.preferences.budget_max is not None
                or state.active_product or state.last_presented_products else 'named_customer_pending_request')
    result = AgentResult(reply_text=copy(template, name=name, topic=topic), intent='commerce',
        response_metadata={'qualification_name_acknowledged':True})
    return mark_sales_result(result, interpretation=interpretation, goal=interpretation.goal,
        response_source='published_qualification_policy', used_openai_responder=False, used_tray=False)


def normalize_ready_requirement(text, interpretation, state, recent_turns):
    if interpretation is None or interpretation.domain != 'commerce':
        return interpretation
    rules = json.loads(policy('catalogAvailabilityRules'))
    current = fold_text(text)
    if re.search(rules['release'], current):
        required = False
    elif re.search(rules['require'], current):
        required = True
    elif not interpretation.domain_change_explicit:
        # Choosing a model is still the same shopping request even when the
        # semantic interpreter marks it as self-contained. Delivery constraints
        # last until explicitly released or the domain changes.
        required = 'ready_to_ship' in state.active_preferences.get('attributes', [])
        if not required:
            for turn in reversed((recent_turns or [])[-rules['historyTurns']:]):
                if turn.get('role') != 'user':
                    continue
                content = fold_text(turn.get('content', ''))
                if re.search(rules['release'], content):
                    break
                if re.search(rules['require'], content):
                    required = True
                    break
    else:
        required = False
    updated = interpretation.model_copy(deep=True)
    updated.preferences.attributes = [a for a in updated.preferences.attributes if a != 'ready_to_ship']
    if required:
        updated.preferences.attributes.append('ready_to_ship')
    return updated


async def recover_mentioned_product(incoming, state, recent_turns, interpretation=None):
    """History identifies a lookup only; the live catalog supplies all facts."""
    if incoming.image_url or state.active_product or state.last_presented_products:
        return state
    rules = json.loads(policy('catalogHistoryRecoveryRules'))
    contextual = bool(interpretation and interpretation.references_previous_context
                      and not interpretation.domain_change_explicit)
    if not contextual and not re.search(rules['followup'], fold_text(incoming.text)):
        return state
    for turn in reversed((recent_turns or [])[-rules['historyTurns']:]):
        if turn.get('role') != 'assistant':
            continue
        content = re.sub(r'https?://\S+', '', str(turn.get('content', '')))
        refs = list(dict.fromkeys(re.findall(rules['reference'], content, re.I)))
        if not refs:
            continue
        if len(refs) != 1:
            return state
        import app.sales_agent as sales
        facts = await sales.execute_tool('search_products', {'reference': refs[0], 'limit': 10, 'page': 1})
        if not isinstance(facts, dict) or facts.get('error'):
            return state
        matches = [p for p in facts.get('products', []) if str(p.get('reference', '')).casefold() == refs[0].casefold()]
        if len(matches) != 1:
            return state
        from app.commerce.commerce_context import product_reference_from_product
        target = product_reference_from_product(matches[0])
        if target:
            updated = state.model_copy(deep=True)
            updated.active_product = target
            updated.product_resolution_state = 'exact_match'
            return updated
        return state
    return state


def normalize_followup(text, interpretation, state, recent_turns=None):
    if interpretation is None or interpretation.domain != 'commerce':
        return interpretation, state
    rules = json.loads(policy('conversationFollowupRules'))
    folded = fold_text(text)
    if (recent_turns and re.search(rules['resumeMediaReference'],folded)
            and not re.search(rules['purchaseCommit'],folded)):
        previous_user=next((t.get('content','') for t in reversed(recent_turns) if t.get('role')=='user'),'')
        if re.search(rules['productMedia'],fold_text(previous_user)):
            updated=interpretation.model_copy(deep=True)
            updated.goal='inspect'
            updated.purchase_action=updated.checkout_action=updated.payment_action=None
            updated.purchase_items=[]
            updated.confirmation='none'
            updated.references_previous_context=True
            updated.reference_type='current_product'
            updated.reference_position=None
            updated.image_request=bool(re.search(rules['productPhoto'],fold_text(previous_user)))
            updated.product_action=None if updated.image_request else 'get_product_link'
            updated.answer_strategy='search_catalog'
            updated.needs_clarification=False
            return updated,state
    if re.fullmatch(rules['interestOnly'], folded):
        updated=interpretation.model_copy(deep=True)
        updated.goal='inspect'
        updated.purchase_action=updated.checkout_action=updated.payment_action=None
        updated.purchase_items=[]
        updated.confirmation='none'
        updated.references_previous_context=True
        updated.answer_strategy='search_catalog'
        updated.needs_clarification=False
        updated.clarification_question=None
        updated._turn_contract_bound=False
        return updated,state
    denies_brand = bool(re.search(rules['deniesBrand'], folded))
    correction = bool(re.search(rules['correctsSearch'], folded))
    alternative = bool(re.search(rules['alternativeSearch'], folded)
                       and interpretation.goal in {'find','recommend'})
    from app.sales.purchase_selection import is_product_information_question
    from app.catalog.specs.preference_normalize import (
        message_states_color,
        message_states_strap_material,
    )
    from app.sales.purchase_selection import is_bare_purchase_closing
    # A fragment that narrows the requested variant is a search refinement,
    # even when the interpreter also attaches a previous list position.
    variant_signal = bool(
        message_states_color(text)
        or message_states_strap_material(text)
        or re.search(r'\b\d{2}(?:[.,]\d+)?\s*mm\b', folded)
    )
    # A short characteristic answer belongs to the live product question even
    # when the semantic interpreter labels the fragment as generic/handoff.
    # This is common for replies such as "Aço", "azul" and "37 mm".
    from app.sales.discovery import _mentioned_watch_brands
    live_product_context = bool(state.active_product or state.last_presented_products)
    contextual_variant_reply = bool(
        live_product_context
        and variant_signal
        and len(folded.split()) <= 8
        and not _mentioned_watch_brands(text)
    )
    semantic_variant_refinement = bool(
        interpretation.goal in {'inspect', 'buy', 'find'}
        and interpretation.references_previous_context
        and not is_product_information_question(text)
        and not interpretation.image_request and not interpretation.product_action
        and not any((interpretation.purchase_action, interpretation.checkout_action,
                     interpretation.order_action, interpretation.payment_action))
        and not is_bare_purchase_closing(text)
        and variant_signal
    )
    contextual_variant_recovery = bool(
        contextual_variant_reply
        and not semantic_variant_refinement
        and not is_product_information_question(text)
        and not interpretation.image_request and not interpretation.product_action
        and not any((interpretation.purchase_action, interpretation.checkout_action,
                     interpretation.order_action, interpretation.payment_action))
        and not is_bare_purchase_closing(text)
    )
    refines_variant = semantic_variant_refinement or contextual_variant_recovery
    browsing_purchase = bool(interpretation.goal == 'buy' and interpretation.answer_strategy == 'search_catalog'
        and not interpretation.references_previous_context
        and not any((interpretation.purchase_action, interpretation.checkout_action, interpretation.payment_action,
                     interpretation.order_action, interpretation.subject.model, interpretation.subject.reference,
                     interpretation.subject.ean))
        and (interpretation.subject.brand or interpretation.subject.product_type))
    stale_identity = any(value and fold_text(value) not in folded
        for value in (interpretation.subject.brand, interpretation.subject.model,
                      interpretation.subject.reference, interpretation.subject.ean))
    fresh = (bool(re.search(rules['freshSearch'], folded))
             and not interpretation.references_previous_context and stale_identity
             and bool(state.active_preferences or state.active_product or state.last_presented_products))
    if not (denies_brand or correction or fresh or refines_variant or browsing_purchase or alternative):
        return interpretation, state
    updated = interpretation.model_copy(deep=True)
    state = state.model_copy(deep=True)
    updated.goal = 'find'
    updated.answer_strategy = 'search_catalog'
    updated.needs_clarification = False
    updated.ready_for_retrieval = updated.enough_information_to_search = True
    updated.clarification_question = None
    updated.reference_type = updated.reference_position = None
    updated.purchase_action = updated.payment_action = updated.checkout_action = None
    updated.information_needed = ['catalog']
    updated._turn_contract_bound = False
    if refines_variant:
        updated._variant_refinement = True
    preserve_variant_context = bool(
        refines_variant
        and (message_states_strap_material(text) or contextual_variant_recovery)
    )
    if not preserve_variant_context:
        state.active_product = None
        state.last_presented_products = []
    state.pending_action = None
    for field in ('brand', 'model', 'reference', 'ean'):
        value = getattr(updated.subject, field)
        if denies_brand or ((fresh or (alternative and field != 'brand'))
                            and value and fold_text(value) not in folded):
            setattr(updated.subject, field, None)
            state.active_preferences.pop('subject_' + field, None)
    if denies_brand:
        updated.preferences.explicit_no_preferences = list(dict.fromkeys(
            [*updated.preferences.explicit_no_preferences, 'brand']))
    return updated, state


async def try_contextual_question(incoming, interpretation, state, recent_turns):
    if interpretation is None or incoming.image_url:
        return None
    rules = json.loads(policy('conversationFollowupRules'))
    folded = fold_text(incoming.text)
    if re.search(rules['storeLink'], folded):
        # The store address is operator-managed, like every customer-facing copy.
        return AgentResult(reply_text=copy('store_link_reply', url=policy('business.store_url')),
            intent='commerce', response_metadata={'domain':'commerce', 'response_source':'published_routing_policy'})
    if interpretation.order_action or interpretation.shipping_action:
        return None
    from app.sales.purchase_selection import is_product_information_question, match_presented_product_from_text
    if interpretation.purchase_action and not is_product_information_question(incoming.text):
        return None
    media = bool(re.search(rules['productMedia'], folded) or interpretation.product_action == 'get_product_link'
                 or interpretation.image_request)
    resume_mention = bool(re.search(json.loads(policy('catalogHistoryRecoveryRules'))['followup'], folded))
    contextual_inspection = interpretation.goal == 'inspect' and interpretation.references_previous_context
    if not (is_product_information_question(incoming.text) or media or resume_mention or contextual_inspection):
        return None
    from app.commerce.commerce_context import resolve_commerce_reference
    clean = interpretation.model_copy(deep=True)
    # Reuse existing ambiguity and missing-photo boundaries before binding a SKU.
    from app.sales.catalog_reference import resolve_catalog_reference
    resolution = await resolve_catalog_reference(message=incoming, interpretation=clean,
        plan={'intent':'product_search','goal':'inspect'}, state=state)
    if resolution.early_result is not None:
        return resolution.early_result
    named = match_presented_product_from_text(incoming.text, state.last_presented_products,
                                              active_product=state.active_product)
    if len(state.last_presented_products) > 1 and not named and not clean.reference_position:
        return None
    target, _ = resolve_commerce_reference(clean, state)
    if target is None:
        target = match_presented_product_from_text(incoming.text, state.last_presented_products,
                                                   active_product=state.active_product)
    if target is None and re.search(rules['deictic'], folded):
        target = state.active_product
        if target is None and len(state.last_presented_products) == 1:
            target = state.last_presented_products[0]
    if target is None:
        return None
    clean.goal = 'inspect'
    clean.reference_type = 'current_product'
    clean.references_previous_context = True
    clean.reference_position = None
    clean.purchase_action = clean.checkout_action = None
    clean.purchase_items = []
    clean.needs_clarification = False
    clean.answer_strategy = 'search_catalog'
    # Inspection must retain the current question's criteria so the responder
    # can explain a mismatch. The live lookup is by identity, not these filters.
    clean.preferences = interpretation.preferences.model_copy(deep=True)
    clean.subject.brand = target.brand
    clean.subject.model = None
    clean.subject.reference = target.reference
    clean.subject.ean = target.ean
    import app.sales_agent as sales
    from app.sales.result_utils import mark_sales_result
    plan = {'intent':'product_search', 'goal':'inspect'}
    if media:
        from app.sales.catalog_media import try_catalog_media
        clean.image_request = bool(interpretation.image_request or re.search(rules['productPhoto'], folded))
        clean.product_action = None if clean.image_request else 'get_product_link'
        return await try_catalog_media(message=incoming, interpretation=clean, plan=plan,
            state=state, purchase_action=None, resolved_product=target, pending_link_requested=not clean.image_request)
    facts = await sales._execute_contextual_product_lookup(clean, target)
    # An unavailable product is still an identified product. Preserve the
    # truthful service answer instead of rewriting it as a failed broad search.
    if facts.safety_reason:
        return mark_sales_result(facts, interpretation=clean, goal='inspect',
            response_source='deterministic_fallback', used_openai_responder=False, used_tray=True)
    reply = await sales._sales_response_with_openai(incoming, plan, facts, clean,
                                                   state=state, recent_turns=recent_turns)
    return mark_sales_result(reply or facts, interpretation=clean, goal='inspect',
        response_source='openai' if reply else 'deterministic_fallback',
        used_openai_responder=reply is not None, used_tray=True)


async def try_availability_question(incoming, interpretation, state, recent_turns):
    """Product lead-time questions are read-only, including an ambiguous list."""
    if (interpretation is None or incoming.image_url or interpretation.order_action
            or state.order_id or state.cart_session_id):
        return None
    from app.sales.purchase_selection import is_product_information_question, match_presented_product_from_text
    rules = json.loads(policy('catalogAvailabilityRules'))
    if not is_product_information_question(incoming.text) or not re.search(rules['question'], fold_text(incoming.text)):
        return None
    named = match_presented_product_from_text(incoming.text, state.last_presented_products, active_product=state.active_product)
    from app.commerce.commerce_context import resolve_commerce_reference
    resolved, _ = resolve_commerce_reference(interpretation, state)
    from app.sales.purchase_selection import parse_list_position_reference
    ambiguous_list = (len(state.last_presented_products) > 1 and not named
                      and not parse_list_position_reference(incoming.text) and not state.active_product)
    targets = (list(state.last_presented_products) if ambiguous_list else
               [named or resolved] if named or resolved else list(state.last_presented_products))
    if not targets and state.active_product:
        targets = [state.active_product]
    if not targets:
        return None
    from app.catalog.retrieval.limits import customer_result_limit
    import app.sales_agent as sales
    import asyncio
    targets = targets[:customer_result_limit()]
    details = await asyncio.gather(*(sales.execute_tool('get_product', {'product_id':p.product_id}) for p in targets))
    if any(not isinstance(p, dict) or p.get('error') for p in details):
        return None
    from app.catalog.retrieval.availability import (commercial_availability_facts,
        product_availability_state, unavailable_product_reply)
    products = [{**p, 'id':target.product_id, '_revalidated':True,
                 'commercial_availability':commercial_availability_facts(p)} for target, p in zip(targets, details)]
    clean = interpretation.model_copy(deep=True)
    clean.goal = 'inspect'
    clean.subject = type(clean.subject)()
    clean.preferences = ProductPreferences()
    clean.purchase_action = clean.checkout_action = clean.shipping_action = None
    clean.purchase_items = []
    clean.needs_clarification = False
    clean.answer_strategy = 'search_catalog'
    base = AgentResult(intent='commerce', commercial_data={'products':products},
        reply_text='\n\n'.join(copy('catalog_availability_item', name=p.get('name') or target.name or '',
            availability=p.get('availability') or copy('catalog_ready_unknown_note'),
            url=p.get('url') or p.get('product_url') or '') for target, p in zip(targets, products)))
    if len(products)==1 and product_availability_state(products[0])=='unavailable':
        from app.sales.result_utils import mark_sales_result
        base.reply_text=(products[0].get('name') or targets[0].name or '')+'\n'+unavailable_product_reply(products)
        base.safety_reason='product_unavailable'
        base.commercial_data['availability_state']='unavailable'
        return mark_sales_result(base,interpretation=clean,goal='inspect',
            response_source='published_availability_policy',used_openai_responder=False,used_tray=True)
    reply = await sales._sales_response_with_openai(incoming, {'intent':'product_inventory','goal':'inspect'},
        base, clean, state=state, recent_turns=recent_turns)
    from app.sales.result_utils import mark_sales_result
    return mark_sales_result(reply or base, interpretation=clean, goal='inspect',
        response_source='openai' if reply else 'grounded_fallback', used_openai_responder=reply is not None, used_tray=True)
