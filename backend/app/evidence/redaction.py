"""Best-effort redaction of credentials from free text before it becomes evidence."""

from __future__ import annotations

import re

REDACTED = "[redacted]"

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|token|password|passwd|secret|api[_-]?key)\b\s*[:=]\s*\S+"
)


def redact_text(value: str) -> str:
    redacted = _BEARER.sub(f"Bearer {REDACTED}", value)
    redacted = _JWT.sub(REDACTED, redacted)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
