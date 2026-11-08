from fastapi import Request

from app.api.errors import UNAVAILABLE_PROBLEM_TYPE, ApiProblem
from app.api.runtime import ApiRuntime


def get_runtime(request: Request) -> ApiRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, ApiRuntime):
        raise ApiProblem(
            status=503,
            problem_type=UNAVAILABLE_PROBLEM_TYPE,
            title="Application runtime unavailable",
            detail="The API runtime has not been configured.",
            code="runtime_unavailable",
        )
    return runtime
