"""Flat HTTP view models and deterministic projections from the domain/store records."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.domain.schemas import (
    ActionOutcome,
    ActionType,
    ApprovalDecision,
    Incident,
    IncidentStatus,
    PolicyDecisionType,
    RestartDeploymentArgs,
    RollbackDeploymentArgs,
    ScaleDeploymentArgs,
    VerificationStatus,
)
from app.policy.config import PolicyConfig
from app.sim.cluster import SimSnapshot
from app.store import Store
from app.store.models import ProposalState, StoredProposal
from app.store.queries import StoreQueries


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentSummary(ApiModel):
    incident_id: str
    scenario_id: str
    status: IncidentStatus
    opened_at: AwareDatetime
    updated_at: AwareDatetime


class IncidentListResponse(ApiModel):
    items: list[IncidentSummary]
    next_cursor: str | None = None


class TimelineEvent(ApiModel):
    event_id: int
    occurred_at: AwareDatetime
    kind: str
    subject: str
    detail: dict[str, object]


class IncidentDetailResponse(IncidentSummary):
    environment_id: str
    current_run_id: str | None
    terminal_reason: str | None
    verification_status: VerificationStatus | None
    proposal_ids: list[str]
    timeline: list[TimelineEvent]


class PreconditionView(ApiModel):
    name: str
    expected: str
    observed: str
    passed: bool
    checked_at: AwareDatetime


class ProposalDetailResponse(ApiModel):
    proposal_id: str
    incident_id: str
    run_id: str
    version: int
    state: ProposalState
    action_type: ActionType
    target_cluster_id: str
    target_namespace: str
    target_kind: str
    target_name: str
    target_uid: str | None
    proposed_replicas: int | None
    proposed_revision: str | None
    current_desired_replicas: int
    current_available_replicas: int
    current_ready_replicas: int
    current_revision: int
    current_resource_version: int
    current_readiness_path: str
    current_readiness_port: str
    expected_state_fingerprint: str
    current_state_fingerprint: str
    state_observed_at: AwareDatetime
    current_state_observed_at: AwareDatetime
    state_changed: bool
    preconditions: list[PreconditionView]
    expected_postconditions: list[str]
    evidence_ids: list[str]
    policy_decision: PolicyDecisionType | None
    policy_reason_codes: list[str]
    policy_version: str
    policy_bundle_digest: str
    expires_at: AwareDatetime


class DecisionRequest(ApiModel):
    decision: ApprovalDecision
    expected_version: StrictInt = Field(ge=1)
    reason: StrictStr | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_rejection_reason(self) -> DecisionRequest:
        if self.decision is ApprovalDecision.REJECT and not (self.reason or "").strip():
            raise ValueError("a rejection requires a non-empty reason")
        if self.decision is ApprovalDecision.APPROVE and self.reason is not None:
            raise ValueError("reason is only valid for a rejection")
        return self


class DecisionResponse(ApiModel):
    proposal_id: str
    approval_id: str
    decision: ApprovalDecision
    approver_principal_id: str
    approver_role: str
    expires_at: AwareDatetime
    proposal_version: int
    already_decided: bool


class ExecuteResponse(ApiModel):
    proposal_id: str
    reason_code: str
    executed: bool
    attempt_id: str | None
    attempt_outcome: ActionOutcome | None
    proposal_state: ProposalState | None
    verification_status: VerificationStatus | None
    writer_operations: int


class ScenarioRunRequest(ApiModel):
    script: Literal["auto", "scale", "restart", "none"] = "auto"


class ScenarioRunResponse(ApiModel):
    scenario_id: str
    run_id: str
    incident_id: str
    incident_status: IncidentStatus
    investigation_status: str
    escalation_reason: str | None
    proposal_id: str | None
    proposal_version: int | None
    policy_decision: PolicyDecisionType | None
    policy_reason_codes: list[str]
    model_calls: int
    tokens_used: int


class DemoMutationRequest(ApiModel):
    desired_replicas: StrictInt | None = Field(default=None, ge=0, le=1)
    readiness_path: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    readiness_port: StrictInt | StrictStr | None = None

    @model_validator(mode="after")
    def require_a_mutation(self) -> DemoMutationRequest:
        if all(
            value is None
            for value in (self.desired_replicas, self.readiness_path, self.readiness_port)
        ):
            raise ValueError("at least one state field must be supplied")
        if self.readiness_path is not None and not self.readiness_path.startswith("/"):
            raise ValueError("readiness_path must start with /")
        if isinstance(self.readiness_port, int) and not 1 <= self.readiness_port <= 65535:
            raise ValueError("readiness_port must be 1-65535 or a named port")
        if self.readiness_port == "":
            raise ValueError("readiness_port must not be empty")
        return self


class DemoMutationResponse(ApiModel):
    incident_id: str
    desired_replicas: int
    available_replicas: int
    ready_replicas: int
    readiness_path: str
    readiness_port: str
    resource_version: int
    state_fingerprint: str


def incident_summary(incident: Incident) -> IncidentSummary:
    return IncidentSummary(
        incident_id=incident.incident_id,
        scenario_id=incident.scenario_id,
        status=incident.status,
        opened_at=incident.opened_at,
        updated_at=incident.updated_at,
    )


def encode_cursor(incident: Incident) -> str:
    payload = json.dumps(
        {"updated_at": incident.updated_at.isoformat(), "incident_id": incident.incident_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(cursor + padding)
        value = json.loads(decoded)
        if not isinstance(value, dict):
            raise ValueError("cursor must be an object")
        updated_at = datetime.fromisoformat(value["updated_at"])
        incident_id = value["incident_id"]
        if updated_at.tzinfo is None or not isinstance(incident_id, str) or not incident_id:
            raise ValueError("cursor fields are invalid")
        return updated_at.astimezone(UTC), incident_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("cursor is malformed") from error


def incident_detail(store: Store, incident: Incident) -> IncidentDetailResponse:
    queries = StoreQueries(store)
    proposals = queries.list_proposals_for_incident(incident.incident_id)
    attempts = queries.list_attempts_for_incident(incident.incident_id)
    proposal_ids = {item.proposal.proposal_id for item in proposals}
    related_subjects = {incident.incident_id, *proposal_ids}
    for proposal_id in proposal_ids:
        approval = store.get_approval_for_proposal(proposal_id)
        if approval is not None:
            related_subjects.add(approval.approval.approval_id)
    related_subjects.update(item.attempt_id for item in attempts)

    timeline: list[TimelineEvent] = []
    for event in store.audit_events():
        linked_proposal = event.detail.get("proposal_id")
        linked_incident = event.detail.get("incident_id")
        if (
            event.subject in related_subjects
            or (isinstance(linked_proposal, str) and linked_proposal in proposal_ids)
            or linked_incident == incident.incident_id
        ):
            timeline.append(
                TimelineEvent(
                    event_id=event.event_id,
                    occurred_at=datetime.fromisoformat(event.occurred_at),
                    kind=event.kind,
                    subject=event.subject,
                    detail=event.detail,
                )
            )

    verification_status: VerificationStatus | None = None
    if incident.status is IncidentStatus.CLOSED:
        verification_status = VerificationStatus.RECOVERED
    elif proposals:
        latest_state = proposals[0].state
        if latest_state is ProposalState.RECOVERED:
            verification_status = VerificationStatus.RECOVERED
        elif latest_state is ProposalState.NOT_RECOVERED:
            verification_status = VerificationStatus.NOT_RECOVERED
        elif (
            latest_state is ProposalState.ESCALATED
            and attempts
            and attempts[-1].outcome is ActionOutcome.EXECUTED
        ):
            verification_status = VerificationStatus.UNKNOWN

    return IncidentDetailResponse(
        **incident_summary(incident).model_dump(),
        environment_id=incident.environment_id,
        current_run_id=incident.current_run_id,
        terminal_reason=incident.terminal_reason,
        verification_status=verification_status,
        proposal_ids=[item.proposal.proposal_id for item in proposals],
        timeline=timeline,
    )


def proposal_detail(
    stored: StoredProposal,
    *,
    current_snapshot: SimSnapshot,
    policy_decision: PolicyDecisionType | None,
    policy_reason_codes: tuple[str, ...],
    policy_config: PolicyConfig,
    now: datetime,
) -> ProposalDetailResponse:
    # Project the infrastructure snapshot into flat response fields.
    snapshot = current_snapshot
    observation = snapshot.observation
    proposal = stored.proposal
    arguments = proposal.arguments
    proposed_replicas: int | None = None
    proposed_revision: str | None = None
    if isinstance(arguments, ScaleDeploymentArgs):
        proposed_replicas = arguments.replicas
    elif isinstance(arguments, RollbackDeploymentArgs):
        proposed_replicas = observation.desired_replicas
        proposed_revision = arguments.revision
    elif isinstance(arguments, RestartDeploymentArgs):
        proposed_replicas = observation.desired_replicas
        proposed_revision = str(snapshot.revision + 1)
    else:
        raise TypeError("proposal contains an unsupported action argument model")

    current_fingerprint = snapshot.state_fingerprint
    state_changed = current_fingerprint != proposal.expected_state_fingerprint
    target = arguments.target
    expected_target = (
        f"{policy_config.allowed_namespace}/{policy_config.allowed_kind}/"
        f"{policy_config.allowed_resource}@{policy_config.target_uid}"
    )
    observed_target = f"{target.namespace}/{target.kind}/{target.name}@{target.uid or 'missing'}"
    preconditions = [
        PreconditionView(
            name="live_state_fingerprint",
            expected=proposal.expected_state_fingerprint,
            observed=current_fingerprint,
            passed=not state_changed,
            checked_at=now,
        ),
        PreconditionView(
            name="proposal_not_expired",
            expected=proposal.expires_at.isoformat(),
            observed=now.isoformat(),
            passed=now < proposal.expires_at,
            checked_at=now,
        ),
        PreconditionView(
            name="target_identity",
            expected=expected_target,
            observed=observed_target,
            passed=(
                target.namespace == policy_config.allowed_namespace
                and target.kind == policy_config.allowed_kind
                and target.name == policy_config.allowed_resource
                and target.uid == policy_config.target_uid
            ),
            checked_at=now,
        ),
    ]
    return ProposalDetailResponse(
        proposal_id=proposal.proposal_id,
        incident_id=proposal.incident_id,
        run_id=proposal.run_id,
        version=stored.version,
        state=stored.state,
        action_type=proposal.action_type,
        target_cluster_id=target.cluster_id,
        target_namespace=target.namespace,
        target_kind=target.kind,
        target_name=target.name,
        target_uid=target.uid,
        proposed_replicas=proposed_replicas,
        proposed_revision=proposed_revision,
        current_desired_replicas=observation.desired_replicas,
        current_available_replicas=observation.available_replicas,
        current_ready_replicas=observation.ready_replicas,
        current_revision=snapshot.revision,
        current_resource_version=snapshot.resource_version,
        current_readiness_path=snapshot.readiness_path,
        current_readiness_port=str(snapshot.readiness_port),
        expected_state_fingerprint=proposal.expected_state_fingerprint,
        current_state_fingerprint=current_fingerprint,
        state_observed_at=proposal.state_observed_at,
        current_state_observed_at=now,
        state_changed=state_changed,
        preconditions=preconditions,
        expected_postconditions=list(proposal.expected_postconditions),
        evidence_ids=list(proposal.source_evidence_ids),
        policy_decision=policy_decision,
        policy_reason_codes=list(policy_reason_codes),
        policy_version=proposal.policy_version,
        policy_bundle_digest=proposal.policy_bundle_digest,
        expires_at=proposal.expires_at,
    )


__all__ = [
    "DecisionRequest",
    "DecisionResponse",
    "DemoMutationRequest",
    "DemoMutationResponse",
    "ExecuteResponse",
    "IncidentDetailResponse",
    "IncidentListResponse",
    "IncidentSummary",
    "PreconditionView",
    "ProposalDetailResponse",
    "ScenarioRunRequest",
    "ScenarioRunResponse",
    "TimelineEvent",
    "decode_cursor",
    "encode_cursor",
    "incident_detail",
    "incident_summary",
    "proposal_detail",
]
