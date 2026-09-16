"""Operator-managed routing and identity corrections before commerce actions."""
from __future__ import annotations

import json
import re
from app.catalog.retrieval.text import fold_text
from app.configuration.runtime import message, policy
from app.models import AgentResult


def preflight_reply(text):
    for rule in json.loads(policy('conversationPreflightRules')):
        if not re.search(rule['pattern'],fold_text(text)):
            continue
        reply=message(rule['message'])
        if rule['action']=='handoff':
            from app.ops.handoff_service import build_human_handoff_result
            return build_human_handoff_result(reason='published_routing_policy',reply_text=reply)
        if rule['action']=='reply':
            return AgentResult(reply_text=reply,intent='commerce',
                response_metadata={'domain':'commerce','response_source':'published_routing_policy'})
    return None


def normalize_identity(interpretation):
    if interpretation is None:
        return None
    aliases=json.loads(policy('catalogIdentityAliases'))
    updated=interpretation.model_copy(deep=True)
    for field,mapping in aliases.items():
        if field not in {'brand','model'}:
            continue
        value=getattr(updated.subject,field)
        canonical=mapping.get(fold_text(value))
        if canonical:
            setattr(updated.subject,field,canonical)
    return updated
