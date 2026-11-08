"""Typed store records and the one lifecycle transition table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.schemas import ActionProposal, Approval, PolicyDecision, PolicyDecisionType


class StoreError(RuntimeError):
    """A refusal with a stable code. The message is for humans, the code is the contract."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    PREFLIGHTED = "preflighted"
    DENIED = "denied"
    AWAITING_APPROVAL = "awaiting_approval"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXECUTION_UNKNOWN = "execution_unknown"
    FAILED = "failed"
    VERIFYING = "verifying"
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    ESCALATED = "escalated"


TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.PROPOSED: frozenset({ProposalState.PREFLIGHTED, ProposalState.DENIED}),
    ProposalState.PREFLIGHTED: frozenset(
        {
            ProposalState.DENIED,
            ProposalState.AWAITING_APPROVAL,
            ProposalState.AUTHORIZED,
        }
    ),
    ProposalState.AWAITING_APPROVAL: frozenset(
        {ProposalState.AUTHORIZED, ProposalState.DENIED}
    ),
    ProposalState.AUTHORIZED: frozenset({ProposalState.EXECUTING}),
    ProposalState.EXECUTING: frozenset(
        {
            ProposalState.EXECUTED,
            ProposalState.EXECUTION_UNKNOWN,
            ProposalState.FAILED,
        }
    ),
    ProposalState.EXECUTED: frozenset({ProposalState.VERIFYING}),
    ProposalState.EXECUTION_UNKNOWN: frozenset(
        {
            ProposalState.EXECUTING,
            ProposalState.EXECUTED,
            ProposalState.VERIFYING,
            ProposalState.FAILED,
        }
    ),
    ProposalState.FAILED: frozenset(),
    ProposalState.VERIFYING: frozenset(
        {
            ProposalState.RECOVERED,
            ProposalState.NOT_RECOVERED,
            ProposalState.ESCALATED,
        }
    ),
    ProposalState.RECOVERED: frozenset(),
    ProposalState.NOT_RECOVERED: frozenset(),
    ProposalState.ESCALATED: frozenset(),
    ProposalState.DENIED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        ProposalState.DENIED,
        ProposalState.FAILED,
        ProposalState.RECOVERED,
        ProposalState.NOT_RECOVERED,
        ProposalState.ESCALATED,
    }
)


def state_after_decision(decision: PolicyDecisionType) -> ProposalState:
    if decision is PolicyDecisionType.DENY:
        return ProposalState.DENIED
    if decision is PolicyDecisionType.REQUIRE_APPROVAL:
        return ProposalState.AWAITING_APPROVAL
    return ProposalState.AUTHORIZED


@dataclass(frozen=True)
class StoredProposal:
    proposal: ActionProposal
    digest: str
    state: ProposalState
    version: int


@dataclass(frozen=True)
class StoredApproval:
    approval: Approval
    version: int
    policy_bundle_digest: str
    environment_id: str
    incident_id: str
    target_identity: str


@dataclass(frozen=True)
class StoredDecision:
    decision: PolicyDecision


@dataclass(frozen=True)
class ApprovalOutcome:
    approval: StoredApproval
    already_decided: bool


@dataclass(frozen=True)
class AuditEvent:
    event_id: int
    occurred_at: str
    kind: str
    subject: str
    detail: dict[str, object]


__all__ = [
    "ApprovalOutcome",
    "AuditEvent",
    "ProposalState",
    "StoreError",
    "StoredApproval",
    "StoredDecision",
    "StoredProposal",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "state_after_decision",
]
