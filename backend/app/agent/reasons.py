"""Stable escalation reason codes for the investigation.

Reason codes are plain strings. The one composed reason validates its provider code against
a closed set first, so no reason is ever built from untrusted text.
"""

from __future__ import annotations

NO_EVIDENCE = "no_evidence"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
PROVIDER_ERROR = "provider_error"
BUDGET_EXHAUSTED_MODEL_CALLS = "budget_exhausted_model_calls"
BUDGET_EXHAUSTED_TOKENS = "budget_exhausted_tokens"
BUDGET_EXHAUSTED_WALL_CLOCK = "budget_exhausted_wall_clock"
INVALID_MODEL_OUTPUT = "invalid_model_output"
UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
UNSUPPORTED_ACTION = "unsupported_action"
INVALID_PARAMETERS = "invalid_parameters"
NO_ACTION_PROPOSED = "no_action_proposed"

PROVIDER_ERROR_CODES = frozenset(
    {"timeout", "rate_limited", "unavailable", "invalid_response", "script_exhausted"}
)


def provider_error_reason(code: str) -> str:
    """Return ``provider_error:<code>`` for a code from the provider's closed set."""
    if code not in PROVIDER_ERROR_CODES:
        raise ValueError(f"unknown provider error code: {code!r}")
    return f"{PROVIDER_ERROR}:{code}"
