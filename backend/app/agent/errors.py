"""Validation failures raised while turning model output into domain objects."""

from __future__ import annotations


class AgentOutputError(ValueError):
    """The model output cannot become a trusted domain object.

    ``reason`` is one of the stable reason codes in :mod:`app.agent.reasons`. The message is
    for humans and may mention rejected values; the reason is what callers route on.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
