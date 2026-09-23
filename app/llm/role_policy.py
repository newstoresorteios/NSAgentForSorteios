"""Per-call controls from the operator catalog, isolated with ContextVar."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from app.configuration.runtime import current_bundle


class RolePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str | None = None
    reasoning_effort: Literal['none','minimal','low','medium','high','xhigh','max','ultra'] | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=128000)
    timeout_seconds: float | None = Field(default=None, gt=0, le=180)
    allow_transport_fallback: bool = False


active_policy: ContextVar[RolePolicy | None] = ContextVar('llm_role_policy', default=None)


def role_for(call_type):
    call_type = call_type.split('_fallback_')[0].removesuffix('_shadow')
    if any(part in call_type for part in ('image', 'vision', 'visual')):
        return 'vision'
    if 'evaluation' in call_type or 'regression' in call_type or 'audit' in call_type:
        return 'evaluation'
    if any(part in call_type for part in ('judge', 'critique', 'double_check', 'learning_reflect')):
        return 'review'
    if call_type in {'decision','structured','product_selection','checkout_repair'}:
        return 'interpretation'
    return 'composition'


def registry():
    raw = current_bundle().get('values', {}).get('modelCapabilityRegistry', '{}')
    return json.loads(raw) if isinstance(raw, str) else raw


@contextmanager
def configured_call(call_type, model, *, structured=False, tools=False):
    raw = current_bundle().get('values', {}).get('modelRolePolicies', '{}')
    policies = json.loads(raw) if isinstance(raw, str) else raw
    role = role_for(call_type)
    config = RolePolicy.model_validate(policies[role]) if role in policies else None
    effective = (config.model or model) if config else model
    if config:
        caps = registry().get(effective)
        if not caps:
            raise ValueError('model_capabilities_not_registered:' + effective)
        if structured and not caps.get('structured_outputs'):
            raise ValueError('model_structured_outputs_unsupported')
        if tools and not caps.get('tools'):
            raise ValueError('model_tools_unsupported')
        if config.reasoning_effort is not None and config.reasoning_effort not in caps.get('reasoning_efforts', []):
            raise ValueError('model_reasoning_effort_unsupported')
        from app.ops.observability import log_event
        log_event('openai.role_policy', {'role':role,'requested_model':model,'effective_model':effective,
            'effort':config.reasoning_effort,'max_output_tokens':config.max_output_tokens})
    token = active_policy.set(config)
    try:
        yield effective, config
    finally:
        active_policy.reset(token)


def apply_chat_controls(kwargs, model, *, tools=False):
    config = active_policy.get()
    if not config:
        from app.config import get_settings
        limit = getattr(get_settings(), 'openai_max_output_tokens', None)
        if limit:
            kwargs['max_completion_tokens'] = int(limit)
        return
    caps = registry()[model]
    if not caps.get('chat_completions') or (tools and not caps.get('chat_tools')):
        raise ValueError('model_chat_transport_unsupported')
    if config.reasoning_effort is not None:
        kwargs['reasoning_effort'] = config.reasoning_effort
    if config.max_output_tokens is not None:
        kwargs['max_completion_tokens'] = config.max_output_tokens
