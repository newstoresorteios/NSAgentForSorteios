"""Manual semantic audit. Suggestions never mutate configuration or code."""
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict
from app.evaluation.conversation_auditor import audit_replay
from app.evaluation.regression_judge import trim_evidence


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['interpretation','retrieval','confirmation','composition','state','integration']
    severity: Literal['critical','major','minor']
    evidence: str
    probable_cause: str
    proposed_test: str
    recommendation: str


class SemanticAudit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    outcome: Literal['passed','failed','inconclusive']
    findings: list[AuditFinding]
    summary: str

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from app.llm.openai_strict_schema import apply_openai_strict_schema
        return apply_openai_strict_schema(handler(core_schema))


async def audit_conversation(replay, expected, *, conversation=None):
    from app.configuration.runtime import message, policy
    from app.llm.openai_gateway import parse_structured_output
    objective = audit_replay(replay, expected)
    try:
        result = await parse_structured_output(model=str(policy('historyEvaluationModel')),
            text_format=SemanticAudit, call_type='conversation_audit',
            messages=[{'role':'system','content':message('conversation_audit_instructions')},
                      {'role':'user','content':json.dumps(trim_evidence({
                          'conversation':conversation or [], 'replay':replay,
                          'expectations':expected.model_dump(mode='json'),
                          'objective_findings':objective['findings']}),ensure_ascii=False,default=str)}],
            timeout_seconds=float(policy('historyEvaluationJudgeTimeout')))
    except Exception as exc:
        return {**objective, 'outcome':'inconclusive', 'assessment':'semantic_incomplete',
                'error_type':type(exc).__name__, 'paid_calls':None}
    semantic=result.parsed.model_dump(mode='json')
    outcome=semantic['outcome']
    if objective['findings'] or semantic['findings']:
        outcome='failed'
    return {**objective, 'assessment':'objective_and_semantic', 'outcome':outcome,
            'semantic':semantic, 'paid_calls':1, 'review_required':True}
