from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_runtime
from app.api.errors import READINESS_PROBLEM_TYPE, ApiProblem, ProblemDetails
from app.api.runtime import ApiRuntime

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    service: str
    status: str


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(service="backend", status="ok")


@router.get("/health/live", response_model=HealthResponse, tags=["system"])
def liveness() -> HealthResponse:
    return HealthResponse(service="backend", status="ok")


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": ProblemDetails}},
    tags=["system"],
)
def readiness(runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> HealthResponse:
    if not runtime.is_ready():
        raise ApiProblem(
            status=503,
            problem_type=READINESS_PROBLEM_TYPE,
            title="Readiness check failed",
            detail="The local store or simulator state is unavailable.",
            code="dependency_unavailable",
        )
    return HealthResponse(service="backend", status="ok")
