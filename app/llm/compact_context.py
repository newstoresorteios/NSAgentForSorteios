"""Lossless deduplication of repeated structured facts; never summarize user data."""
from collections import Counter
from copy import deepcopy
import json


def compact_payload(payload):
    def encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    counts = Counter()
    def count(value):
        if isinstance(value, dict):
            key = encoded(value)
            if len(key) >= 200:
                counts[key] += 1
            for v in value.values(): count(v)
        elif isinstance(value, list):
            for v in value: count(v)
    count(payload)
    evidence, refs = {}, {}
    def visit(value):
        if isinstance(value, dict):
            key = encoded(value)
            if counts[key] > 1:
                if key not in refs:
                    ref = 'fact_' + str(len(refs) + 1)
                    refs[key] = ref
                    evidence[ref] = deepcopy(value)
                return {'_context_ref':refs[key]}
            return {k:visit(v) for k,v in value.items()}
        if isinstance(value, list): return [visit(v) for v in value]
        return value
    compacted = {'context':visit(payload), 'context_evidence':evidence}
    return compacted if evidence and len(encoded(compacted)) < len(encoded(payload)) else payload


def serialize_context(payload, **kwargs):
    from app.configuration.runtime import current_bundle
    if not current_bundle().get('values', {}).get('compactRoleContextEnabled'):
        return json.dumps(payload, **kwargs)
    from app.ops.observability import log_event
    compacted = compact_payload(payload)
    if compacted is not payload:
        from app.configuration.runtime import message
        compacted['context_reference_instructions'] = message('context_reference_instructions')
    before, after = json.dumps(payload, **kwargs), json.dumps(compacted, **kwargs)
    if len(after) >= len(before):
        after = before
    log_event('prompt.context_compaction', {'before_chars':len(before), 'after_chars':len(after),
        'saved_chars':len(before)-len(after), 'measurement':'characters_not_tokens'})
    return after
