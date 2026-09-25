from fastapi import FastAPI

from app.api.errors import ProblemDetails, register_exception_handlers
from app.api.health import router as health_router
from app.api.incidents import router as incidents_router

app = FastAPI(
    title="Faultweld",
    version="0.1.3",
    responses={422: {"model": ProblemDetails}},
)

app.include_router(health_router)
app.include_router(incidents_router)
register_exception_handlers(app)
