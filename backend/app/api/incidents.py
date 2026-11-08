from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import get_runtime
from app.api.errors import NOT_FOUND_PROBLEM_TYPE, ApiProblem, ProblemDetails
from app.api.runtime import ApiRuntime
from app.api.views import (
    IncidentDetailResponse,
    IncidentListResponse,
    decode_cursor,
    encode_cursor,
    incident_detail,
    incident_summary,
)
from app.store.queries import StoreQueries

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=IncidentListResponse)
def list_incidents(
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> IncidentListResponse:
    """List incidents newest-first with an opaque keyset cursor."""
    with runtime.open_store() as store:
        items = list(StoreQueries(store).list_incidents())
    if cursor is not None:
        try:
            marker = decode_cursor(cursor)
        except ValueError as error:
            raise ApiProblem(
                status=422,
                problem_type="urn:faultweld:problem:request-validation",
                title="Invalid cursor",
                detail="The incident cursor is malformed.",
                code="invalid_cursor",
            ) from error
        items = [item for item in items if (item.updated_at, item.incident_id) < marker]
    page = items[: limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = encode_cursor(page[-1]) if has_more and page else None
    return IncidentListResponse(
        items=[incident_summary(item) for item in page],
        next_cursor=next_cursor,
    )


@router.get(
    "/{incident_id}",
    response_model=IncidentDetailResponse,
    responses={404: {"model": ProblemDetails}},
)
def get_incident(
    incident_id: Annotated[str, Path(min_length=1, max_length=128)],
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
) -> IncidentDetailResponse:
    with runtime.open_store() as store:
        incident = store.get_incident(incident_id)
        if incident is None:
            raise ApiProblem(
                status=404,
                problem_type=NOT_FOUND_PROBLEM_TYPE,
                title="Incident not found",
                detail="The requested incident does not exist.",
                code="incident_not_found",
            )
        return incident_detail(store, incident)
