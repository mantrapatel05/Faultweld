"""Stable reason codes for store, lifecycle and approval failures.

The gateway reuses these codes in commit 5. They are part of the observable contract, so
add codes freely but never rename one.
"""

from __future__ import annotations

ALREADY_REVOKED = "already_revoked"
APPROVAL_NOT_FOUND = "approval_not_found"
CONFLICTING_DECISION = "conflicting_decision"
CORRUPT_ROW = "corrupt_row"
DIGEST_MISMATCH = "digest_mismatch"
DUPLICATE_ATTEMPT_KEY = "duplicate_attempt_key"
DUPLICATE_INCIDENT = "duplicate_incident"
DUPLICATE_PROPOSAL = "duplicate_proposal"
ILLEGAL_TRANSITION = "illegal_transition"
INCIDENT_NOT_FOUND = "incident_not_found"
INVALID_ENVIRONMENT = "invalid_environment"
INVALID_REASON = "invalid_reason"
POLICY_CHANGED = "policy_changed"
POLICY_DENIED = "policy_denied"
PROPOSAL_EXPIRED = "proposal_expired"
PROPOSAL_MISMATCH = "proposal_mismatch"
PROPOSAL_NOT_AWAITING_APPROVAL = "proposal_not_awaiting_approval"
PROPOSAL_NOT_FOUND = "proposal_not_found"
ROLE_NOT_PERMITTED = "role_not_permitted"
SELF_APPROVAL = "self_approval"
STALE_VERSION = "stale_version"
STATE_CHANGED_SINCE_PROPOSAL = "state_changed_since_proposal"
STATE_UNAVAILABLE = "state_unavailable"
UNKNOWN_PRINCIPAL = "unknown_principal"

__all__ = [
    "ALREADY_REVOKED",
    "APPROVAL_NOT_FOUND",
    "CONFLICTING_DECISION",
    "CORRUPT_ROW",
    "DIGEST_MISMATCH",
    "DUPLICATE_ATTEMPT_KEY",
    "DUPLICATE_INCIDENT",
    "DUPLICATE_PROPOSAL",
    "ILLEGAL_TRANSITION",
    "INCIDENT_NOT_FOUND",
    "INVALID_ENVIRONMENT",
    "INVALID_REASON",
    "POLICY_CHANGED",
    "POLICY_DENIED",
    "PROPOSAL_EXPIRED",
    "PROPOSAL_MISMATCH",
    "PROPOSAL_NOT_AWAITING_APPROVAL",
    "PROPOSAL_NOT_FOUND",
    "ROLE_NOT_PERMITTED",
    "SELF_APPROVAL",
    "STALE_VERSION",
    "STATE_CHANGED_SINCE_PROPOSAL",
    "STATE_UNAVAILABLE",
    "UNKNOWN_PRINCIPAL",
]
