from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path

from app.api.dependencies import get_runtime
from app.api.errors import ProblemDetails
from app.api.runtime import ApiRuntime
from app.api.views import ScenarioRunRequest, ScenarioRunResponse

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.post(
    "/{scenario_id}/runs",
    response_model=ScenarioRunResponse,
    responses={404: {"model": ProblemDetails}, 422: {"model": ProblemDetails}},
)
def create_scenario_run(
    scenario_id: Annotated[str, Path(min_length=1, max_length=64)],
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
    payload: Annotated[ScenarioRunRequest | None, Body()] = None,
) -> ScenarioRunResponse:
    outcome = runtime.run_scenario(scenario_id, payload.script if payload else "auto")
    return ScenarioRunResponse(
        scenario_id=scenario_id,
        run_id=outcome.run_id,
        incident_id=outcome.incident.incident_id,
        incident_status=outcome.incident.status,
        investigation_status=outcome.investigation_status,
        escalation_reason=outcome.escalation_reason,
        proposal_id=(
            outcome.proposal.proposal.proposal_id if outcome.proposal is not None else None
        ),
        proposal_version=outcome.proposal.version if outcome.proposal is not None else None,
        policy_decision=outcome.policy_decision,
        policy_reason_codes=list(outcome.policy_reason_codes),
        model_calls=outcome.model_calls,
        tokens_used=outcome.tokens_used,
    )
