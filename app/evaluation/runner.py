from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from uuid import uuid4

from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle, policy
from app.config import get_settings
from app.evaluation.context import EvaluationContext, bind_evaluation, reset_evaluation
from app.evaluation.judge import compact_metadata, effective_outcome, judge_replay
from app.evaluation import repository
from app.ops.runtime_context import set_current_turn, reset_current_turn
from app.ops.turn_runtime import TurnRuntimeContext, LLMCallBudget


def bounded(name, lower, upper):
    return max(lower, min(upper, int(policy(name))))


def replay_state(case, *, now=None):
    """Preserve the age of session memory at the original customer turn."""
    state = deepcopy(case['initial_state'])
    recorded = case.get('recorded_at')
    if not recorded:
        return state
    recorded = datetime.fromisoformat(recorded.replace('Z', '+00:00'))
    recorded = recorded.replace(tzinfo=timezone.utc) if recorded.tzinfo is None else recorded
    delta = (now or datetime.now(timezone.utc)) - recorded
    for key in ('last_browse_at', 'cart_context_updated_at'):
        if state.get(key):
            value = datetime.fromisoformat(str(state[key]).replace('Z', '+00:00'))
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            state[key] = (value + delta).isoformat()
    return state


async def replay_case(case, persona, *, fixtures=None):
    from app.message_pipeline import process_incoming_message
    from app.models import IncomingMessage

    conversation_id = 'evaluation:' + uuid4().hex
    history = [{**turn, 'conversation_id': conversation_id} for turn in case['history']]
    state = replay_state(case)
    state['last_conversation_id'] = conversation_id
    context = EvaluationContext(case['workspace_id'], persona, history=history, state=state,
                                fixtures=fixtures, max_tool_calls=bounded('historyEvaluationMaxTools', 1, 60))
    runtime = TurnRuntimeContext(trace_id=conversation_id, llm_budget=LLMCallBudget(max_calls=10, enforce=True))
    token = bind_evaluation(context)
    runtime_token = set_current_turn(runtime)
    started = time.perf_counter()
    result = None
    error = None
    try:
        incoming = IncomingMessage(text=case['input'], channel=case['channel'], conversation_id=conversation_id)
        result = await asyncio.wait_for(process_incoming_message(incoming, {'found': False}),
                                        timeout=bounded('historyEvaluationTurnTimeout', 10, 180))
    except Exception as exc:
        error = type(exc).__name__
    finally:
        reset_current_turn(runtime_token)
        reset_evaluation(token)
    metadata = result.response_metadata if result else {}
    report = {
        'reply': result.reply_text if result else '', 'handoff': bool(result and result.handoff_required),
        'safety_reason': result.safety_reason if result else None,
        'metadata': compact_metadata(metadata), 'tools': context.tool_calls, 'blocked': context.blocked,
        'review_inputs': context.review_inputs,
        'session_time_rebased': bool(case.get('recorded_at')),
        'runtime': runtime.safe_summary(), 'error': error,
        'integration_errors': [call.get('error_type') or 'tool_error' for call in (c['result'] for c in context.tool_calls) if call.get('error')],
        'real_model_calls': sum(1 for call in runtime.openai_calls if call.get('ok')),
        'generative_exercised': any(call.get('ok') for call in runtime.openai_calls),
        'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
    }
    return report, context.captured


async def evaluate_case(workspace_id, case_id, request_id, *, repair=True):
    from app.persona.persona_runtime import load_persona_runtime
    from app.learning.constitution import check_instruction_delta

    case = repository.get_case(workspace_id, case_id)
    persona = await asyncio.to_thread(load_persona_runtime, workspace_id=workspace_id)
    if not persona.enabled or str(persona.workspace_id) != str(workspace_id):
        raise ValueError('evaluation_persona_unavailable')
    bundle = persona.configuration_bundle
    if not int(bundle.get('version') or 0):
        raise ValueError('evaluation_configuration_not_published')
    settings = settings_from_bundle(get_settings(), bundle)
    if not settings.openai_api_key:
        raise ValueError('evaluation_model_not_configured')
    versions = {'persona': persona.persona_version_id, 'configuration': bundle['version'],
                'configuration_hash': hashlib.sha256(json.dumps(bundle['values'], sort_keys=True).encode()).hexdigest(),
                'case': case['fingerprint'], 'model': settings.openai_model,
                'code': os.getenv('VERCEL_GIT_COMMIT_SHA') or os.getenv('GIT_COMMIT_SHA') or 'working_tree',
                'deployment': os.getenv('VERCEL_URL') or 'local',
                'catalog': 'current_read_only', 'mode': 'historical_context_replay'}
    if not repository.start_run(workspace_id, case_id, request_id, versions):
        return {'id': request_id, 'status': 'already_requested'}
    binding = bind_bundle(bundle, settings)
    # Evaluation calls have their own budget, never the middleware/customer budget.
    outer_runtime = set_current_turn(None)
    report = {'input': case['input'], 'historical_reply': case['historical_reply'], 'versions': versions}
    try:
        baseline, fixtures = await replay_case(case, persona)
        report['replay'] = baseline
        verdict = await judge_replay(case, baseline, persona)
        report['assessment'] = verdict.model_dump(mode='json')
        report['outcome'] = effective_outcome(baseline, verdict)
        report['repair'] = {'status': 'not_needed', 'applied': False}
        if report['outcome'] == 'failed' and repair and policy('historyEvaluationRepairEnabled'):
            instruction = verdict.repair_instruction.strip()
            target = str(policy('historyEvaluationRepairTarget'))
            field = next((f for f in bundle['fields'] if f['key'] == target), {})
            allowed, reason = check_instruction_delta(instruction, max_chars=bounded('historyEvaluationRepairMaxChars', 100, 2000))
            if field.get('target') != 'message' or not target.startswith('message.instructions.') or '{' in instruction or '}' in instruction:
                allowed, reason = False, 'invalid_repair_target_or_template'
            report['repair'] = {'status': 'rejected', 'applied': False, 'target': target, 'instruction': instruction, 'reason': reason}
            if allowed:
                candidate = persona.model_copy(deep=True)
                value = str(bundle['values'][target]) + '\n' + instruction
                if len(value) > int(field.get('maxLength') or 30000):
                    report['repair']['reason'] = 'candidate_too_long'
                else:
                    candidate.configuration_bundle['values'][target] = value
                    candidate.runtime_configuration[target] = value
                    candidate_bundle = candidate.configuration_bundle
                    candidate_binding = bind_bundle(candidate_bundle, settings_from_bundle(settings, candidate_bundle))
                    try:
                        candidate_replay, _ = await replay_case(case, candidate, fixtures=fixtures)
                        candidate_verdict = await judge_replay(case, candidate_replay, candidate)
                    finally:
                        reset_bundle(candidate_binding)
                    outcome = effective_outcome(candidate_replay, candidate_verdict)
                    report['repair'].update(status='verified_candidate' if outcome == 'passed' else 'rejected',
                        reason=None if outcome == 'passed' else outcome, replay=candidate_replay,
                        assessment=candidate_verdict.model_dump(mode='json'), proposed_value=value)
        repository.finish_run(workspace_id, request_id, 'completed', report)
        return {'id': request_id, 'status': 'completed', **report}
    except Exception as exc:
        report.update(error=type(exc).__name__, outcome='inconclusive')
        repository.finish_run(workspace_id, request_id, 'error', report)
        return {'id': request_id, 'status': 'error', **report}
    finally:
        reset_current_turn(outer_runtime)
        reset_bundle(binding)
