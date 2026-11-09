"""Stable model-client errors; provider response details are never exposed."""

from __future__ import annotations

from typing import Literal

ClientErrorCode = Literal[
    "timeout", "rate_limited", "unavailable", "invalid_response", "auth_failed"
]


class LlmClientError(RuntimeError):
    def __init__(self, code: ClientErrorCode, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
