from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import AwareDatetime, BaseModel

from app.api.errors import NOT_FOUND_PROBLEM_TYPE, ApiProblem, ProblemDetails
from app.domain.schemas import IncidentStatus, NonEmptyId

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


class IncidentSummary(BaseModel):
    incident_id: NonEmptyId
    scenario_id: NonEmptyId
    status: IncidentStatus
    opened_at: AwareDatetime
    updated_at: AwareDatetime


class IncidentListResponse(BaseModel):
    items: list[IncidentSummary]
    next_cursor: str | None = None


@router.get("", response_model=IncidentListResponse, responses={422: {"model": ProblemDetails}})
async def list_incidents(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> IncidentListResponse:
    del limit, cursor
    return IncidentListResponse(items=[])


@router.get(
    "/{incident_id}",
    response_model=IncidentSummary,
    responses={404: {"model": ProblemDetails}, 422: {"model": ProblemDetails}},
)
async def get_incident(
    incident_id: Annotated[str, Path(min_length=1)],
) -> IncidentSummary:
    raise ApiProblem(
        status=404,
        problem_type=NOT_FOUND_PROBLEM_TYPE,
        title="Incident not found",
        detail="The requested incident does not exist in the local read-only store.",
        code="incident_not_found",
    )
