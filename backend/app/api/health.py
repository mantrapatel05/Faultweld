from fastapi import APIRouter
from pydantic import BaseModel

from app.api.errors import READINESS_PROBLEM_TYPE, ApiProblem, ProblemDetails

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    service: str
    status: str


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(service="backend", status="ok")


@router.get("/health/live", response_model=HealthResponse, tags=["system"])
async def liveness() -> HealthResponse:
    return HealthResponse(service="backend", status="ok")


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": ProblemDetails}},
    tags=["system"],
)
async def readiness() -> HealthResponse:
    raise ApiProblem(
        status=503,
        problem_type=READINESS_PROBLEM_TYPE,
        title="Readiness check failed",
        detail="Persistence is not configured for this foundation build.",
        code="dependency_unavailable",
    )
