"""Typed input and output contracts for deterministic Faultweld safety evaluations.

The runner records observations only. Evaluator expectations deliberately live in a
separate file and are loaded by :mod:`app.evals.graders`, never by this module or runner.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtraEvidenceSetup(EvalModel):
    key: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=500)
    freshness: Literal["fresh", "stale", "missing", "unknown"] = "fresh"
    trust: Literal["trusted", "untrusted"] = "untrusted"


class ModelActionSetup(EvalModel):
    """One proposal response; multiple entries exercise bounded correction retries."""

    action_type: str | None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ("live",)
    rationale: str = "restore the expected service health"


class FixtureCaseSetup(EvalModel):
    case_id: str = Field(min_length=1)
    scenario_id: str = "healthy-replica-loss"
    model_actions: tuple[ModelActionSetup, ...] = (
        ModelActionSetup(action_type="scale_deployment", parameters={"replicas": 1}),
    )
    diagnosis_evidence_refs: tuple[str, ...] = ("live",)
    extra_evidence: tuple[ExtraEvidenceSetup, ...] = ()
    provider_error: Literal["timeout", "rate_limited", "unavailable"] | None = None
    max_model_calls: int = Field(default=4, ge=0)
    max_tokens: int = Field(default=8000, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    advance_after_investigation_ticks: int = Field(default=0, ge=0)
    repeat_approval: bool = False
    approval_actor: Literal["reviewer", "observer", "agent"] = "reviewer"
    caller: Literal["proposal", "wrong"] = "proposal"
    mutate_after_approval: Literal["none", "replicas", "probe_path", "probe_port"] = "none"
    tamper_proposal_after_approval: bool = False
    revoke_approval_after_approval: bool = False
    expire_approval_after_approval: bool = False
    policy_version_override: str | None = None
    required_approval_roles_override: tuple[str, ...] | None = None
    writer_fault: Literal["none", "timeout_before_write", "apply_then_timeout"] = "none"
    verifier_unavailable: bool = False
    delivery: Literal["single", "sequential", "concurrent"] = "single"


class FixtureSetup(EvalModel):
    fixture_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(pattern=r"^[1-9][0-9]*$")
    description: str = Field(min_length=1, max_length=300)
    cases: tuple[FixtureCaseSetup, ...] = Field(min_length=1)


class StateObservation(EvalModel):
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    pod_present: bool
    readiness_path: str
    readiness_port: str | int
    revision: int
    resource_version: int
    tick: int


class WriterOperationObservation(EvalModel):
    operation_type: str
    expected_resource_version: int
    before_resource_version: int
    after_resource_version: int
    replicas: int | None = None
    revision: str | None = None


class AuthorizationObservation(EvalModel):
    """Facts visible to the runner; these are not evaluator expectations."""

    approval_present: bool
    approval_is_approve: bool
    approval_not_revoked: bool
    approval_not_consumed: bool
    approval_not_expired: bool
    proposal_not_expired: bool
    approver_role_permitted: bool
    caller_matches_proposal: bool
    proposal_digest_matches_stored: bool
    proposal_digest_matches_approval: bool
    target_identity_matches_approval: bool
    policy_version_matches: bool
    policy_bundle_matches: bool
    current_policy_requires_approval: bool
    state_fingerprint_matches_approval: bool
    state_fingerprint_matches_proposal: bool

    @property
    def all_current(self) -> bool:
        return all(self.model_dump().values())


class CaseRawResult(EvalModel):
    case_id: str
    scenario_id: str
    investigation_status: str
    escalation_reason: str | None = None
    model_calls: int = 0
    tokens_used: int = 0
    cited_evidence_ids: tuple[str, ...] = ()
    proposal_evidence_ids: tuple[str, ...] = ()
    evidence_ids_by_key: dict[str, str] = Field(default_factory=dict)
    proposal_id: str | None = None
    proposal_action: str | None = None
    policy_version: str | None = None
    policy_bundle_digest: str | None = None
    gateway_policy_version: str | None = None
    gateway_policy_bundle_digest: str | None = None
    proposal_replicas: int | None = None
    policy_decision: str | None = None
    policy_reason_codes: tuple[str, ...] = ()
    approval_error: str | None = None
    approval_decision: str | None = None
    second_approval_result: str | None = None
    approval_presented_to_gateway: bool = False
    authorization: AuthorizationObservation | None = None
    gateway_reason_codes: tuple[str, ...] = ()
    state_at_gateway: StateObservation | None = None
    approval_consumed_after_gateway: bool | None = None
    attempt_count: int = 0
    audit_event_kinds: tuple[str, ...] = ()
    attempt_outcomes: tuple[str, ...] = ()
    idempotency_key: str | None = None
    writer_operations: tuple[WriterOperationObservation, ...] = ()
    proposal_state: str | None = None
    incident_status: str
    verification_status: str | None = None
    verification_reason: str | None = None
    verification_checks: dict[str, bool | None] = Field(default_factory=dict)
    runner_error_class: str | None = None
    final_state: StateObservation | None


class FixtureRawResult(EvalModel):
    schema_version: Literal["1"] = "1"
    fixture_id: str
    fixture_version: str
    repeat_index: int = Field(ge=1)
    provider: Literal["scripted"] = "scripted"
    git_sha: str
    cases: tuple[CaseRawResult, ...]


class GradedCaseResult(EvalModel):
    fixture_id: str
    case_id: str
    passed: bool
    failures: tuple[str, ...] = ()


class GradedFixtureResult(EvalModel):
    fixture_id: str
    repeat_index: int
    passed: bool
    cases: tuple[GradedCaseResult, ...]


class FixtureMetrics(EvalModel):
    pass_k: bool
    stale_approvals_rejected: str
    forbidden_mutations: int
    duplicate_side_effects: int
    false_recovery_count: int
    valid_action_rejections: int
    unauthorized_gateway_calls_blocked: str


class EvaluationMetrics(EvalModel):
    stale_approvals_rejected: str
    forbidden_mutations: int
    duplicate_side_effects: int
    false_recovery_count: int
    valid_action_rejections: int
    unauthorized_gateway_calls_blocked: str
    pass_k: str
    deterministic_repeats: bool


class EvaluationReport(EvalModel):
    schema_version: Literal["1"] = "1"
    git_sha: str
    provider: Literal["scripted"] = "scripted"
    repeats: int
    policy_versions: tuple[str, ...]
    policy_bundle_digests: tuple[str, ...]
    fixture_versions: dict[str, str]
    fixture_metrics: dict[str, FixtureMetrics]
    fixture_results: tuple[GradedFixtureResult, ...]
    metrics: EvaluationMetrics
    failures: tuple[str, ...]
    limitations: tuple[str, ...]
    reproduce_command: str
