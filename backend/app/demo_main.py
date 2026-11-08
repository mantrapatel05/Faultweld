"""Separate local-only application that exposes the demo mutation control."""

from app.api.application import create_app

app = create_app(include_demo_routes=True)
