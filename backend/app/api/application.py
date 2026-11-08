from fastapi import FastAPI

from app.api.demo import router as demo_router
from app.api.errors import ProblemDetails, register_exception_handlers
from app.api.health import router as health_router
from app.api.incidents import router as incidents_router
from app.api.proposals import router as proposals_router
from app.api.runtime import ApiRuntime
from app.api.scenarios import router as scenarios_router


def create_app(
    runtime: ApiRuntime | None = None,
    *,
    include_demo_routes: bool = False,
) -> FastAPI:
    application = FastAPI(
        title="Faultweld",
        version="0.2.0",
        responses={422: {"model": ProblemDetails}},
    )
    application.state.runtime = runtime or ApiRuntime()
    application.include_router(health_router)
    application.include_router(incidents_router)
    application.include_router(proposals_router)
    application.include_router(scenarios_router)
    if include_demo_routes:
        application.include_router(demo_router)
    register_exception_handlers(application)
    return application
