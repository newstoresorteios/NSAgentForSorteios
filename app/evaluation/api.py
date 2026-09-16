from __future__ import annotations

from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.security import verify_admin_token
from app.evaluation import repository

router = APIRouter(prefix='/api/admin/evaluations', tags=['historical-evaluation'], dependencies=[Depends(verify_admin_token)])


class ImportRequest(BaseModel):
    workspace_id: UUID
    limit: int | None = Field(default=None, ge=1, le=100)


class RunRequest(BaseModel):
    workspace_id: UUID
    case_id: UUID
    request_id: UUID = Field(default_factory=uuid4)
    repair: bool = True


@router.post('/import')
def import_history(body: ImportRequest):
    from app.persona.persona_runtime import load_persona_runtime
    persona = load_persona_runtime(workspace_id=str(body.workspace_id))
    values = persona.configuration_bundle.get('values') or {}
    if str(persona.workspace_id) != str(body.workspace_id) or not persona.enabled:
        raise HTTPException(404, 'evaluation_persona_unavailable')
    limit = min(body.limit or int(values['historyEvaluationBatchSize']), int(values['historyEvaluationBatchSize']), 100)
    cases = repository.historical_cases(str(body.workspace_id), limit=max(1, limit),
        history_turns=max(1, min(40, int(values['historyEvaluationHistoryTurns']))),
        lookback_days=max(1, min(365, int(values['historyEvaluationLookbackDays']))))
    return {'items': repository.import_cases(str(body.workspace_id), cases)}


@router.get('/{workspace_id}')
def list_evaluations(workspace_id: UUID):
    return repository.list_results(str(workspace_id))


@router.post('/run')
async def run_evaluation(body: RunRequest):
    from app.evaluation.runner import evaluate_case
    try:
        return await evaluate_case(str(body.workspace_id), str(body.case_id), str(body.request_id), repair=body.repair)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
