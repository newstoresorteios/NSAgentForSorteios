"""Ground search-state changes in customer words, not inferred indifference."""
import re

from app.catalog.retrieval.text import _fold

DIMENSIONS = {
    'brand': r'marca', 'color': r'cor|mostrador', 'style': r'estilo',
    'mechanism': r'movimento', 'crystal': r'vidro|cristal',
    'strap': r'pulseira|bracelete', 'material': r'material',
    'budget': r'orcamento|preco|valor', 'case_size': r'tamanho|caixa',
    'gender': r'genero', 'occasion': r'ocasiao', 'purchase_purpose': r'finalidade',
}


def released_dimensions(text, last_slot=None):
    folded = _fold(text)
    released = set()
    for slot, noun in DIMENSIONS.items():
        if re.search(r'\b(?:qualquer\s+(?:' + noun + r')|(?:tanto faz|indiferente)\s+(?:a |o )?(?:' + noun + r')|(?:' + noun + r')\s+(?:tanto faz|indiferente)|'
                     r'(?:sem\s+preferencia|nao tenho preferencia)\s+(?:(?:de|por|para|quanto a|quanto ao)\s+)?(?:' + noun + r'))\b', folded):
            released.add(slot)
    if last_slot in DIMENSIONS and re.fullmatch(r'(?:tanto faz|sem preferencia|qualquer um|nao sei)[.! ]*', folded):
        released.add(last_slot)
    if re.search(r'\b(?:outras marcas|qualquer marca|sem marca definida)\b', folded):
        released.add('brand')
    if 'strap' in released:
        released.add('material')
    return released


def reconcile_search_turn(interpretation, state, text, recent_turns=None):
    if interpretation is None or interpretation.domain != 'commerce':
        return interpretation
    from app.sales.dialogue_phase import is_fresh_commerce_start, is_generic_catalog_ask
    from app.catalog.specs.preference_normalize import extract_stated_style
    prefs = interpretation.preferences
    # A movement/calibre code is a property, not the watch SKU.
    calibre = re.search(r'\b(?:movimento|motor|calibre|caliber)\s*:?\s*([a-z]*\d[a-z0-9-]*)\b', _fold(text))
    if calibre:
        code = calibre.group(1)
        for field in ('model', 'reference'):
            if _fold(getattr(interpretation.subject, field)) == code:
                setattr(interpretation.subject, field, None)
        attribute = 'calibre ' + code
        if attribute not in prefs.attributes:
            prefs.attributes.append(attribute)
    prior = dict(getattr(state, 'active_preferences', None) or {})
    last = next((t for t in reversed(recent_turns or []) if t.get('role') == 'assistant'), {})
    slot = ((last.get('metadata') or {}).get('discovery_question') or {}).get('slot')
    released = released_dimensions(text, slot)
    if re.search(r'\bsem nenhuma preferencia\b', _fold(text)):
        released.update(prefs.explicit_no_preferences)
    continuing = bool(prior and not interpretation.domain_change_explicit
                      and not is_fresh_commerce_start(text) and not is_generic_catalog_ask(text)
                      and (interpretation.references_previous_context
                           or re.search(r'\b(?:mantenha|mantendo|o restante|agora prefiro)\b', _fold(text))))
    # A different explicitly identified product does not inherit a prior SKU's requirements.
    if interpretation.subject.reference and prior.get('subject_reference') and (
        interpretation.subject.reference.casefold() != str(prior['subject_reference']).casefold()
    ):
        continuing = False
    inherited_no = set(prior.get('explicit_no_preferences') or []) if continuing else set()
    allowed_no = inherited_no | released
    # An explicit replacement takes precedence over previously relaxed facets.
    from app.catalog.specs.requirements import feature_rules, _contains, _denied
    rules = feature_rules()
    for field in ('color', 'style', 'occasion', 'material', 'mechanism', 'crystal'):
        value = getattr(prefs, field)
        stated = bool(value and _contains(text, str(value)))
        if field in {'mechanism', 'crystal'}:
            stated = any(r['field'] == field and any(_contains(text, a) for a in r['aliases'])
                         and not _denied(text, r, 'catalogFeatureNegationPhrases') for r in rules)
        if stated and field not in released:
            allowed_no.discard(field)
    if interpretation.subject.brand and 'brand' not in released:
        allowed_no.discard('brand')
    prefs.explicit_no_preferences = sorted(allowed_no)
    if continuing:
        interpretation.references_previous_context = True
        if not interpretation.subject.brand and 'brand' not in allowed_no:
            interpretation.subject.brand = prior.get('subject_brand')
        for field in ('budget_max', 'budget_min', 'color', 'style', 'occasion', 'material', 'mechanism', 'crystal'):
            dimension = 'budget' if field.startswith('budget_') else field
            if dimension not in allowed_no and getattr(prefs, field) is None:
                setattr(prefs, field, prior.get(field))
        # Keep unrelated requirements when a follow-up changes just one facet.
        # Structured preferences carry continuity; stale free-form attributes can
        # contradict an explicit replacement (automatic -> quartz), so do not
        # merge a historical bag of words into the current interpretation.
        for attr in prior.get('attributes') or []:
            if (str(attr).startswith('required_strap_material:')
                    and not {'strap', 'material'} & allowed_no
                    and prefs.material == prior.get('material') and attr not in prefs.attributes):
                prefs.attributes.append(attr)
        if interpretation.payment_method_preference is None:
            interpretation.payment_method_preference = prior.get('budget_payment_basis')
    style = extract_stated_style(text)
    if style and 'style' not in released and not re.search(r'\bnao\s+(?:quero\s+)?' + re.escape(_fold(style)), _fold(text)):
        prefs.style = style
        prefs.explicit_no_preferences = [v for v in prefs.explicit_no_preferences if v != 'style']
    if re.search(r'\b(?:no|via|por|em)\s+pix\b', _fold(text)):
        interpretation.payment_method_preference = 'pix'
    elif re.search(r'\b(?:no cartao|parcelad[oa]|a prazo)\b', _fold(text)):
        interpretation.payment_method_preference = 'card'
    for dimension in allowed_no:
        if dimension in {'mechanism', 'crystal', 'style', 'color', 'material', 'occasion'}:
            setattr(prefs, dimension, None)
        elif dimension == 'brand':
            interpretation.subject.brand = None
        elif dimension == 'budget':
            prefs.budget_max = prefs.budget_min = None
    if 'mechanism' in allowed_no:
        prefs.attributes = [a for a in prefs.attributes if not re.search(r'automatic|quartzo|quartz|solar|eco.?drive|mecanic', _fold(a))]
        interpretation._technical_requirements.pop('mechanism', None)
    if 'strap' in allowed_no or 'material' in allowed_no:
        prefs.attributes = [a for a in prefs.attributes if not re.search(r'pulseira|bracelete|required_strap_material:', _fold(a))]
    if 'crystal' in allowed_no:
        prefs.attributes = [a for a in prefs.attributes if not re.search(r'safira|sapphire|mineral|hardlex|cristal|vidro', _fold(a))]
        interpretation._technical_requirements.pop('crystal', None)
    return interpretation
