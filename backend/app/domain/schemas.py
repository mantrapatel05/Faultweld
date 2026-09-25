from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

SCHEMA_VERSION = "0.1"
type SchemaVersion = Literal["0.1"]
NonEmptyId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
Confidence = Annotated[float, Field(ge=0, le=1)]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: SchemaVersion


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    ACTION_PROPOSED = "action_proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"
    CLOSED = "closed"
    ESCALATED = "escalated"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class EvidenceSourceType(StrEnum):
    KUBERNETES = "kubernetes"
    TELEMETRY = "telemetry"
    LOGS = "logs"
    TRACES = "traces"
    RUNBOOK = "runbook"
    CHANGE_HISTORY = "change_history"
    OTHER = "other"


class EvidenceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"


class PolicyDecisionType(StrEnum):
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW = "allow"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ActionOutcome(StrEnum):
    EXECUTED = "executed"
    EXECUTION_UNKNOWN = "execution_unknown"
    FAILED = "failed"


class VerificationStatus(StrEnum):
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"


class ResourceRef(DomainModel):
    cluster_id: NonEmptyId
    namespace: NonEmptyId
    kind: NonEmptyId
    name: NonEmptyId
    uid: NonEmptyId | None = None


class PolicyConstraints(DomainModel):
    allowed_namespace: NonEmptyId
    allowed_resource: NonEmptyId
    min_replicas: NonNegativeInt | None = None
    max_replicas: NonNegativeInt | None = None
    max_attempts: PositiveInt

    @model_validator(mode="after")
    def validate_replica_bounds(self) -> PolicyConstraints:
        if (
            self.min_replicas is not None
            and self.max_replicas is not None
            and self.min_replicas > self.max_replicas
        ):
            raise ValueError("min_replicas cannot exceed max_replicas")
        return self


class RunBudget(DomainModel):
    max_wall_clock_seconds: PositiveInt
    max_model_calls: NonNegativeInt
    max_tool_calls: NonNegativeInt
    max_tokens: NonNegativeInt
    max_cost_usd: NonNegativeFloat


class NormalizedAlert(DomainModel):
    alert_id: NonEmptyId
    source: NonEmptyId
    summary: str
    observed_at: AwareDatetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class Incident(DomainModel):
    incident_id: NonEmptyId
    scenario_id: NonEmptyId
    environment_id: NonEmptyId
    status: IncidentStatus
    normalized_alert: NormalizedAlert
    opened_at: AwareDatetime
    updated_at: AwareDatetime
    current_run_id: NonEmptyId | None = None
    terminal_reason: str | None = None


class AgentRun(DomainModel):
    run_id: NonEmptyId
    incident_id: NonEmptyId
    status: AgentRunStatus
    workflow_version: NonEmptyId
    model_provider_id: NonEmptyId
    model_id: NonEmptyId
    prompt_config_revision: NonEmptyId
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    budgets: RunBudget
    trace_id: NonEmptyId


class Evidence(DomainModel):
    evidence_id: NonEmptyId
    incident_id: NonEmptyId
    run_id: NonEmptyId
    source_type: EvidenceSourceType
    source_resource: NonEmptyId
    query: str
    observed_at: AwareDatetime
    freshness: EvidenceFreshness
    content_hash: Sha256
    payload_reference: NonEmptyId
    retrieval_score: Confidence | None = None
    runbook_revision: NonEmptyId | None = None


class DiagnosisHypothesis(DomainModel):
    hypothesis_id: NonEmptyId
    statement: str
    confidence: Confidence
    supporting_evidence_ids: tuple[NonEmptyId, ...]
    contradicting_evidence_ids: tuple[NonEmptyId, ...] = ()


class Diagnosis(DomainModel):
    diagnosis_id: NonEmptyId
    run_id: NonEmptyId
    hypotheses: tuple[DiagnosisHypothesis, ...] = Field(min_length=1)
    missing_evidence: tuple[NonEmptyId, ...] = ()
    confidence: Confidence
    is_sufficient: bool


class ActionArgumentsBase(DomainModel):
    target: ResourceRef


class RestartDeploymentArgs(ActionArgumentsBase):
    action_type: Literal[ActionType.RESTART_DEPLOYMENT] = ActionType.RESTART_DEPLOYMENT


class RollbackDeploymentArgs(ActionArgumentsBase):
    action_type: Literal[ActionType.ROLLBACK_DEPLOYMENT] = ActionType.ROLLBACK_DEPLOYMENT
    revision: NonEmptyId


class ScaleDeploymentArgs(ActionArgumentsBase):
    action_type: Literal[ActionType.SCALE_DEPLOYMENT] = ActionType.SCALE_DEPLOYMENT
    replicas: NonNegativeInt


type ActionArguments = Annotated[
    RestartDeploymentArgs | RollbackDeploymentArgs | ScaleDeploymentArgs,
    Field(discriminator="action_type"),
]


class RollbackPlan(DomainModel):
    action_type: ActionType
    description: str


class ActionProposal(DomainModel):
    proposal_id: NonEmptyId
    incident_id: NonEmptyId
    run_id: NonEmptyId
    principal_id: NonEmptyId
    environment_id: NonEmptyId
    action_type: ActionType
    arguments: ActionArguments
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expected_state_fingerprint: Sha256
    state_observed_at: AwareDatetime
    policy_version: NonEmptyId
    policy_bundle_digest: Sha256
    source_evidence_ids: tuple[NonEmptyId, ...]
    runbook_revision_ids: tuple[NonEmptyId, ...] = ()
    expected_postconditions: tuple[NonEmptyId, ...] = Field(min_length=1)
    rollback: RollbackPlan | None = None
    canonical_parameter_digest: Sha256
    idempotency_key: NonEmptyId
    attempt_limit: PositiveInt

    @model_validator(mode="after")
    def validate_action_consistency(self) -> ActionProposal:
        if self.arguments.action_type != self.action_type:
            raise ValueError("action_type must match the typed action arguments")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class PolicyDecision(DomainModel):
    decision_id: NonEmptyId
    proposal_id: NonEmptyId
    decision: PolicyDecisionType
    reason_codes: tuple[NonEmptyId, ...] = Field(min_length=1)
    policy_version: NonEmptyId
    policy_bundle_digest: Sha256
    constraints: PolicyConstraints
    required_approval_roles: tuple[NonEmptyId, ...] = ()


class Approval(DomainModel):
    approval_id: NonEmptyId
    proposal_id: NonEmptyId
    proposal_digest: Sha256
    approver_principal_id: NonEmptyId
    approver_role: NonEmptyId
    decision: ApprovalDecision
    decided_at: AwareDatetime
    expires_at: AwareDatetime
    policy_version: NonEmptyId
    state_fingerprint: Sha256
    consumed: bool = False
    revoked: bool = False

    @model_validator(mode="after")
    def validate_expiry(self) -> Approval:
        if self.expires_at <= self.decided_at:
            raise ValueError("expires_at must be after decided_at")
        return self


class ActionPreflightSnapshot(DomainModel):
    target: ResourceRef
    state_fingerprint: Sha256
    observed_at: AwareDatetime
    proposal_digest: Sha256


class ActionAttempt(DomainModel):
    attempt_id: NonEmptyId
    proposal_id: NonEmptyId
    idempotency_key: NonEmptyId
    attempt_number: PositiveInt
    preflight: ActionPreflightSnapshot
    authorization_decision_id: NonEmptyId
    operation_started_at: AwareDatetime
    operation_finished_at: AwareDatetime
    outcome: ActionOutcome
    before_resource_version: NonEmptyId | None = None
    after_resource_version: NonEmptyId | None = None
    sanitized_error_class: str | None = None
    trace_id: NonEmptyId

    @model_validator(mode="after")
    def validate_operation_window(self) -> ActionAttempt:
        if self.operation_finished_at < self.operation_started_at:
            raise ValueError("operation_finished_at cannot precede operation_started_at")
        return self


class VerificationCheck(DomainModel):
    check_id: NonEmptyId
    source: NonEmptyId
    observed_at: AwareDatetime
    passed: bool | None
    observed_value: str | None = None
    expected_value: str | None = None


class VerificationResult(DomainModel):
    verification_id: NonEmptyId
    incident_id: NonEmptyId
    attempt_id: NonEmptyId
    status: VerificationStatus
    checks: tuple[VerificationCheck, ...] = Field(min_length=1)
    observed_at: AwareDatetime
    reason_code: NonEmptyId | None = None

    @model_validator(mode="after")
    def validate_status_against_checks(self) -> VerificationResult:
        if self.status == VerificationStatus.RECOVERED and any(
            check.passed is not True for check in self.checks
        ):
            raise ValueError("recovered verification requires all checks to pass")
        if self.status == VerificationStatus.NOT_RECOVERED and not any(
            check.passed is False for check in self.checks
        ):
            raise ValueError("not_recovered verification requires a failed check")
        if self.status == VerificationStatus.UNKNOWN and any(
            check.passed is not None for check in self.checks
        ):
            raise ValueError("unknown verification cannot contain passed or failed checks")
        return self
