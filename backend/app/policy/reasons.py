"""Stable reason codes for policy decisions.

These strings are part of the contract between the policy, the store, the gateway and the
demo CLI. Add a code; never rename one that has shipped.
"""

from __future__ import annotations

ACTION_NOT_ALLOWED = "action_not_allowed"
ACTION_NOT_SUPPORTED = "action_not_supported"
ALLOWED = "allowed"
APPROVAL_REQUIRED = "approval_required"
ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
ENVIRONMENT_NOT_ALLOWED = "environment_not_allowed"
INCIDENT_CLOSED = "incident_closed"
MISSING_INPUT = "missing_input"
NAMESPACE_NOT_ALLOWED = "namespace_not_allowed"
PROPOSAL_EXPIRED = "proposal_expired"
REPLICAS_OUT_OF_BOUNDS = "replicas_out_of_bounds"
TARGET_NOT_ALLOWED = "target_not_allowed"
TARGET_UID_MISMATCH = "target_uid_mismatch"

__all__ = [
    "ACTION_NOT_ALLOWED",
    "ACTION_NOT_SUPPORTED",
    "ALLOWED",
    "APPROVAL_REQUIRED",
    "ATTEMPT_LIMIT_REACHED",
    "ENVIRONMENT_NOT_ALLOWED",
    "INCIDENT_CLOSED",
    "MISSING_INPUT",
    "NAMESPACE_NOT_ALLOWED",
    "PROPOSAL_EXPIRED",
    "REPLICAS_OUT_OF_BOUNDS",
    "TARGET_NOT_ALLOWED",
    "TARGET_UID_MISMATCH",
]
