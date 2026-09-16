from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from app.security import verify_admin_token
from app.evaluation.regression_models import RegressionSuite
from app.evaluation import regression_repository as repository

router=APIRouter(prefix='/api/admin/regression',tags=['conversation-regression'],dependencies=[Depends(verify_admin_token)])


class SuiteRequest(BaseModel):
    workspace_id:UUID
    specification:RegressionSuite


class TurnRequest(BaseModel):
    workspace_id:UUID
    suite_id:UUID
    scenario_key:str
    run_id:UUID
    step_index:int=Field(ge=0,le=11)


@router.post('/suites')
def register(body:SuiteRequest):
    keys=[s.key for s in body.specification.scenarios]
    if len(set(keys))!=len(keys) or any(s.category not in body.specification.categories for s in body.specification.scenarios):
        raise HTTPException(422,'invalid_scenario_identity_or_category')
    try:
        return repository.register_suite(str(body.workspace_id),body.specification.model_dump(mode='json'))
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post('/turn')
async def turn(body:TurnRequest):
    from app.evaluation.regression_runner import run_turn
    try:
        return await run_turn(str(body.workspace_id),str(body.suite_id),body.scenario_key,str(body.run_id),body.step_index)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/{workspace_id}/runs/{run_id}')
def get_run(workspace_id:UUID,run_id:UUID):
    from app.evaluation.regression_runner import public_run
    try:
        return public_run(repository.get_run(str(workspace_id),str(run_id)))
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
