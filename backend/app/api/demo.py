"""Demo-only mutation controls. This router is never mounted by app.main."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.dependencies import get_runtime
from app.api.errors import ProblemDetails
from app.api.runtime import ApiRuntime
from app.api.views import DemoMutationRequest, DemoMutationResponse

router = APIRouter(prefix="/api/demo", tags=["demo"], include_in_schema=True)


@router.post(
    "/{incident_id}/mutate",
    response_model=DemoMutationResponse,
    responses={404: {"model": ProblemDetails}, 422: {"model": ProblemDetails}},
)
def mutate_demo_state(
    incident_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: DemoMutationRequest,
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
) -> DemoMutationResponse:
    runtime.mutate_demo_state(
        incident_id,
        desired_replicas=payload.desired_replicas,
        readiness_path=payload.readiness_path,
        readiness_port=payload.readiness_port,
    )
    snapshot = runtime.cluster.source.snapshot()
    return DemoMutationResponse(
        incident_id=incident_id,
        desired_replicas=snapshot.observation.desired_replicas,
        available_replicas=snapshot.observation.available_replicas,
        ready_replicas=snapshot.observation.ready_replicas,
        readiness_path=snapshot.readiness_path,
        readiness_port=str(snapshot.readiness_port),
        resource_version=snapshot.resource_version,
        state_fingerprint=snapshot.state_fingerprint,
    )
