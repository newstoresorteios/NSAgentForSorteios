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
    for key, value in incoming.items():
        if value not in (None, '', [], {}) and key not in {'attributes', 'explicit_no_preferences'}:
            no_preferences.discard('budget' if key in {'budget_min', 'budget_max'} else key)
    no_preferences.update(incoming.get('explicit_no_preferences') or [])
    for slot in no_preferences:
        for key in ('budget_min', 'budget_max') if slot == 'budget' else (slot,):
            merged.pop(key, None)
    merged['explicit_no_preferences'] = sorted(no_preferences)
    return merged
