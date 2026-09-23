from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.configuration.runtime import message, policy
from app.llm.openai_gateway import parse_structured_output


class Finding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: Literal['understanding', 'history', 'retrieval', 'response', 'persona', 'handoff', 'infrastructure', 'evidence']
    severity: Literal['critical', 'major', 'minor']
    evidence: str
    explanation: str
    suggested_fix: str


class EvaluationVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    historical_outcome: Literal['passed', 'failed', 'inconclusive']
    current_outcome: Literal['passed', 'failed', 'inconclusive']
    summary: str
    findings: list[Finding]
    repair_instruction: str

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from app.llm.openai_strict_schema import apply_openai_strict_schema
        return apply_openai_strict_schema(handler(core_schema))


def compact_metadata(metadata):
    fields = ('interpretation', 'active_preferences', 'response_critique', 'final_response_validation',
              'factual_validation', 'decision_snapshot', 'grounded_commerce_evidence', 'response_source',
              'product_resolution_state', 'answer_council_final_validation', 'persona_runtime', 'commerce_state',
              'technical_requirements', 'technical_evidence', 'answer_council',
              'institutional_evidence', 'rejected_candidates', 'missing_evidence', 'image_candidate_comparisons',
              'rejected_draft_factual_validation', 'factual_validation_repaired', 'handoff', 'double_check',
              'discovery_question', 'adaptive_discovery')
    result = {key: metadata.get(key) for key in fields if key in metadata}
    runtime = metadata.get('turn_runtime') or {}
    result['path'] = {key: runtime.get(key) for key in ('catalog_queries', 'tray_tools', 'llm_calls_by_type',
                                                      'fallback_reasons', 'integration_failures', 'stage_durations_ms')}
    return result


async def judge_replay(case, replay, persona):
    payload = {'history': case['history'], 'customer': case['input'],
               'initial_state': case['initial_state'],
               'historical': {'reply': case['historical_reply'], 'metadata': compact_metadata(case['historical_metadata'])},
               'current': replay, 'persona': persona.flow_params_dict(),
               'context_complete': case['context_complete']}
    result = await parse_structured_output(model=str(policy('historyEvaluationModel')), text_format=EvaluationVerdict,
        messages=[{'role': 'system', 'content': message('history_evaluation_judge')},
                  {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str)}],
        temperature=0, call_type='historical_evaluation', timeout_seconds=float(policy('historyEvaluationJudgeTimeout')))
    return result.parsed


def effective_outcome(replay, verdict):
    # A forbidden operation can reveal a real intent error, even when the
    # sandbox safely stopped it. Missing fixtures alone remain inconclusive.
    if verdict.current_outcome == 'failed' and any(
        f.stage == 'understanding' and f.severity == 'critical' for f in verdict.findings
    ):
        return 'failed'
    if replay.get('error') or replay.get('blocked') or replay.get('integration_errors'):
        return 'inconclusive'
    if not replay.get('reply', '').strip():
        return 'failed'
    critique = (replay.get('metadata') or {}).get('response_critique') or {}
    if (critique.get('review_status') == 'unavailable'
            and any(v.get('pass_check') is False for v in critique.get('verdicts') or [])
            and not critique.get('applied_factual_fallback')):
        return 'failed'
    # Deterministic greetings can pass behavior checks. The report separately
    # records whether this case exercised generation.
    final = (replay.get('metadata') or {}).get('final_response_validation') or {}
    if final.get('rejected_issues'):
        return 'failed'
    return verdict.current_outcome
