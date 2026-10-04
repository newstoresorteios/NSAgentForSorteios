"""Scoped legacy knowledge and explicit customer memory for the direct engine."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.memory.memory_models import MemoryProposal, MemoryKind
from app.memory.memory_policy import evaluate_memory_proposal, memory_keys_equivalent

KINDS = {
    'preferred_name': 'preferred_name', 'communication_style': 'communication_style',
    'preferred_brands': 'brand_preference', 'preferred_color': 'color_preference',
    'preferred_material': 'material_preference', 'preferred_size': 'size_preference',
    'preferred_style': 'product_preference', 'preferred_price_max': 'price_preference',
    'do_not_repeat': 'do_not_repeat',
}


def learned_context(persona, incoming):
    from app.persona.instruction_extension_repository import list_active_extensions, select_approved_extensions
    from app.learning.cases import list_active_cases
    from app.persona.instruction_policy import validate_instruction
    from app.ops.observability import log_event
    result = {'instructions': [], 'lessons': []}
    try:
        rows = list_active_extensions(tenant_id=persona.tenant_id, workspace_id=persona.workspace_id,
                                     channel=incoming.channel, sender_key=incoming.sender_key, limit=20)
        result['instructions'] = [{'id': row['id'], 'text': row['instruction_text']}
                                  for row in select_approved_extensions(rows)]
        # Only corrections, never another customer's excerpt or identifiers.
        for row in list_active_cases(tenant_id=persona.tenant_id, workspace_id=persona.workspace_id, limit=5):
            correction = str(row.get('correction') or '')[:800]
            if correction and validate_instruction(correction, [])['status'] == 'passed':
                result['lessons'].append({'id': row['id'], 'text': correction})
    except Exception as exc:
        log_event('direct.learned_context.unavailable', {'error_type': type(exc).__name__})
    return result


def save_preference(*, incoming, workspace, tenant, key, action, evidence, preview=False, state=None):
    """Store the user's own words; never model-invented facts or volatile commerce."""
    from app.memory.contact_memory_repository import get_active_contact_memories, upsert_contact_memory, forget_contact_memory
    text = incoming.text or ''
    evidence = evidence.strip()
    if not workspace or not tenant or not incoming.sender_key:
        return {'ok': False, 'error': 'memory_identity_required'}
    if key not in KINDS or not evidence or evidence.casefold() not in text.casefold():
        return {'ok': False, 'error': 'current_message_evidence_required'}
    if action == 'forget':
        if not re.search(r'\b(esque[çc]a|esquecer|apague|remova|n[aã]o guarde|n[aã]o lembre)\b', evidence, re.I):
            return {'ok': False, 'error': 'explicit_forget_request_required'}
    elif not re.search(r'\b(eu|prefiro|gosto|quero|chame|chamo|pode me chamar|n[aã]o repita|meu or[çc]amento|minha prefer[eê]ncia)\b', evidence, re.I):
        return {'ok': False, 'error': 'explicit_preference_required'}
    start = text.casefold().find(evidence.casefold())
    if re.search(r'\b(n[aã]o|nunca|nem)\s*$', text[max(0,start-12):start], re.I):
        return {'ok': False, 'error': 'negation_must_be_preserved'}
    proposal = MemoryProposal(action='forget' if action == 'forget' else 'upsert', key=key,
        kind=MemoryKind(KINDS[key]), value=evidence, safe_summary=evidence,
        confidence=1, importance=.8, ttl_days=60, use_in_instructions=True,
        reason_code='explicit_user_forget_request' if action == 'forget' else 'explicit_user_preference')
    current = [] if preview else get_active_contact_memories(tenant_id=tenant, workspace_id=workspace, sender_key=incoming.sender_key)
    decision = evaluate_memory_proposal(proposal=proposal, inbound=incoming, current_memories=current,
                                       tenant_id=tenant, sender_key=incoming.sender_key)
    if not decision.accepted:
        return {'ok': decision.rejection_codes == ['duplicate'], 'error': 'memory_policy_rejected' if decision.rejection_codes != ['duplicate'] else None}
    legacy = {'preferred_color': 'color_preference', 'preferred_material': 'material_preference',
              'preferred_size': 'size_preference', 'preferred_style': 'style_preference',
              'preferred_price_max': 'price_preference'}
    aliases = tuple(dict.fromkeys((*memory_keys_equivalent(key), legacy.get(key, key))))
    if preview:
        for alias in aliases:
            state.pop(alias, None)
        if action != 'forget':
            state[key] = evidence
        return {'ok': True, 'saved': action != 'forget', 'forgotten': action == 'forget', 'preview': True}
    # Direct explicit memory has its own switch; reuse the legacy sanitizer, scope
    # and repositories, not the legacy speculative auto-apply workflow.
    from app.config import get_settings
    if not get_settings().direct_memory_enabled:
        return {'ok': False, 'error': 'memory_disabled'}
    if action == 'forget':
        for alias in aliases:
            forget_contact_memory(tenant_id=tenant, workspace_id=workspace, sender_key=incoming.sender_key, memory_key=alias)
    else:
        upsert_contact_memory(tenant_id=tenant, workspace_id=workspace, sender_key=incoming.sender_key,
            memory_key=key, memory_kind=KINDS[key], value=evidence, safe_summary=evidence,
            source='explicit_user', metadata={'engine': 'direct', 'evidence': 'current_message'},
            importance=.8, confidence=1, use_in_instructions=True,
            source_inbound_id=incoming.raw.get('inbound_id'), expires_at=datetime.now(timezone.utc)+timedelta(days=60))
        for alias in aliases:
            if alias != key:
                forget_contact_memory(tenant_id=tenant, workspace_id=workspace, sender_key=incoming.sender_key, memory_key=alias)
    return {'ok': True, 'saved': action != 'forget', 'forgotten': action == 'forget'}
