"""Separate local-only application that exposes the demo mutation control."""

from app.api.application import create_app
from app.api.runtime import ApiRuntime

app = create_app(
    ApiRuntime("file:faultweld-browser-demo?mode=memory&cache=shared"),
    include_demo_routes=True,
)
