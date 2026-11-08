from collections.abc import Mapping
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.store.models import StoreError

PROBLEM_MEDIA_TYPE = "application/problem+json"
VALIDATION_PROBLEM_TYPE = "urn:faultweld:problem:request-validation"
NOT_FOUND_PROBLEM_TYPE = "urn:faultweld:problem:not-found"
METHOD_NOT_ALLOWED_PROBLEM_TYPE = "urn:faultweld:problem:method-not-allowed"
READINESS_PROBLEM_TYPE = "urn:faultweld:problem:readiness"
CONFLICT_PROBLEM_TYPE = "urn:faultweld:problem:conflict"
FORBIDDEN_PROBLEM_TYPE = "urn:faultweld:problem:forbidden"
UNAVAILABLE_PROBLEM_TYPE = "urn:faultweld:problem:unavailable"


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str


class ApiProblem(Exception):
    def __init__(self, status: int, problem_type: str, title: str, detail: str, code: str) -> None:
        super().__init__(detail)
        self.status = status
        self.problem_type = problem_type
        self.title = title
        self.detail = detail
        self.code = code


def problem_response(
    request: Request,
    status: int,
    problem_type: str,
    title: str,
    detail: str,
    code: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = ProblemDetails(
        type=problem_type,
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
    )
    return JSONResponse(
        content=problem.model_dump(),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


async def api_problem_handler(request: Request, exc: Exception) -> JSONResponse:
    problem = cast(ApiProblem, exc)
    return problem_response(
        request,
        problem.status,
        problem.problem_type,
        problem.title,
        problem.detail,
        problem.code,
    )


async def store_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StoreError, exc)
    not_found = {
        "incident_not_found",
        "proposal_not_found",
        "approval_not_found",
        "scenario_not_found",
    }
    forbidden = {"self_approval", "role_not_permitted", "unknown_principal"}
    unavailable = {"state_unavailable"}
    if error.code in not_found:
        status, problem_type, title = 404, NOT_FOUND_PROBLEM_TYPE, "Resource not found"
    elif error.code in forbidden:
        status, problem_type, title = 403, FORBIDDEN_PROBLEM_TYPE, "Operation forbidden"
    elif error.code in unavailable:
        status, problem_type, title = 503, UNAVAILABLE_PROBLEM_TYPE, "Dependency unavailable"
    elif error.code in {"invalid_reason", "invalid_scenario_script"}:
        status, problem_type, title = 422, VALIDATION_PROBLEM_TYPE, "Invalid request"
    else:
        status, problem_type, title = 409, CONFLICT_PROBLEM_TYPE, "Operation conflict"
    return problem_response(request, status, problem_type, title, str(error), error.code)


async def request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return problem_response(
        request,
        422,
        VALIDATION_PROBLEM_TYPE,
        "Request validation failed",
        "The request did not match the API contract.",
        "request_validation_error",
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    if http_exc.status_code == 404:
        return problem_response(
            request,
            404,
            NOT_FOUND_PROBLEM_TYPE,
            "Resource not found",
            "The requested resource was not found.",
            "not_found",
            http_exc.headers,
        )
    if http_exc.status_code == 405:
        return problem_response(
            request,
            405,
            METHOD_NOT_ALLOWED_PROBLEM_TYPE,
            "Method not allowed",
            "The requested method is not allowed for this resource.",
            "method_not_allowed",
            http_exc.headers,
        )
    return problem_response(
        request,
        http_exc.status_code,
        "urn:faultweld:problem:http-error",
        "HTTP request failed",
        "The request could not be completed.",
        "http_error",
        http_exc.headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.add_exception_handler(StoreError, store_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
