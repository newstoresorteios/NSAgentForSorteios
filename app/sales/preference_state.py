"""Incremental preferences within a commercial objective; resets live at its boundary."""
from copy import deepcopy


def merge_preferences(prior, incoming):
    merged = deepcopy(prior or {})
    incoming = incoming or {}
    # Empty model defaults do not revoke a previously explicit constraint.
    for key, value in incoming.items():
        if value not in (None, '', [], {}) and key not in {'attributes', 'explicit_no_preferences'}:
            merged[key] = deepcopy(value)
    attrs = list(merged.get('attributes') or [])
    for attr in incoming.get('attributes') or []:
        # Typed facets replace their old value; independent constraints survive.
        if ':' in attr:
            prefix = attr.rsplit(':', 1)[0] + ':'
            attrs = [old for old in attrs if not old.startswith(prefix)]
        if attr not in attrs:
            attrs.append(attr)
    merged['attributes'] = attrs
    no_preferences = set(merged.get('explicit_no_preferences') or [])
    if incoming.get('attributes'):
        no_preferences.discard('attributes')
    for key, value in incoming.items():
        if value not in (None, '', [], {}) and key not in {'attributes', 'explicit_no_preferences'}:
            no_preferences.discard('budget' if key in {'budget_min', 'budget_max'} else key)
    no_preferences.update(incoming.get('explicit_no_preferences') or [])
    for slot in no_preferences:
        for key in ('budget_min', 'budget_max') if slot == 'budget' else (slot,):
            merged.pop(key, None)
        if slot == 'attributes':
            merged['attributes'] = []
    merged['explicit_no_preferences'] = sorted(no_preferences)
    return merged


def update_preference_provenance(prior, before, after, context=None):
    """Changed fields get the current evidence; retained fields keep their origin.

    Confidence is unknown unless supplied by a calibrated producer. Legacy rows
    are explicitly unknown instead of being attributed to today's customer turn.
    """
    context = context or {}
    provenance = deepcopy(prior or {})
    excluded = {'explicit_no_preferences'}
    for key in (set(before) | set(after)) - excluded:
        old, new = before.get(key), after.get(key)
        if old == new and key in provenance:
            continue
        changed = old != new
        if not changed and new in (None, '', [], {}):
            continue
        confidence = context.get('confidence') if changed else None
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            confidence = None
        provenance[key] = {
            'source': context.get('source', 'unknown') if changed else 'legacy_unknown',
            'inbound_id': context.get('inbound_id') if changed else None,
            'observed_at': context.get('observed_at') if changed else None,
            'time_source': context.get('time_source', 'unavailable') if changed else 'unavailable',
            'confidence': confidence,
            'status': 'removed' if new in (None, '', [], {}) else 'active',
        }
    return provenance
