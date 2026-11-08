"""Fail-closed action gateway with atomic approval consumption and idempotent replay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue

from app.agent.ids import IdFactory, uuid_id
from app.agent.proposal import proposal_digest
from app.domain.schemas import (
    ActionAttempt,
    ActionOutcome,
    ActionPreflightSnapshot,
    ActionProposal,
    ApprovalDecision,
    IncidentStatus,
    PolicyDecisionType,
    RestartDeploymentArgs,
    RollbackDeploymentArgs,
    ScaleDeploymentArgs,
)
from app.evidence.canonical import canonical_json
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.sim.cluster import EvidenceSource, SimSnapshot
from app.sim.ports import (
    ClusterWriter,
    PreconditionFailed,
    RestartOperation,
    RollbackOperation,
    ScaleOperation,
    UnsupportedOperation,
    WriteOperation,
    WriteResult,
)
from app.store import Store, StoreError
from app.store import reasons as store_reasons
from app.store.models import ProposalState, StoredApproval, StoredProposal

APPROVAL_REVOKED = "approval_revoked"
EXECUTION_UNKNOWN = "execution_unknown"
PRECONDITION_FAILED = "precondition_failed"
PRINCIPAL_MISMATCH = "principal_mismatch"
REPLAYED = "replayed"
STATE_CHANGED = "state_changed"
TARGET_MISMATCH = "target_mismatch"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class GatewayResult:
    reason_code: str
    attempt: ActionAttempt | None = None
    write_result: WriteResult | None = None

    @property
    def executed(self) -> bool:
        return self.attempt is not None and self.attempt.outcome is ActionOutcome.EXECUTED


class Gateway:
    def __init__(
        self,
        store: Store,
        source: EvidenceSource,
        writer: ClusterWriter,
        policy_config: PolicyConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
        ids: IdFactory = uuid_id,
    ) -> None:
        self._store = store
        self._source = source
        self._writer = writer
        self._policy_config = policy_config
        self._clock = clock
        self._ids = ids

    def execute(self, proposal_id: str, caller_principal_id: str) -> GatewayResult:
        stored = self._store.get_proposal(proposal_id)
        if stored is None:
            return self._reject(proposal_id, store_reasons.PROPOSAL_NOT_FOUND)
        approval = self._store.get_approval_for_proposal(proposal_id)
        if approval is None:
            return self._reject(proposal_id, store_reasons.APPROVAL_NOT_FOUND)
        if caller_principal_id != stored.proposal.principal_id:
            return self._reject(proposal_id, PRINCIPAL_MISMATCH)

        previous = self._store.get_attempt_by_key(stored.proposal.idempotency_key)
        if previous is not None:
            if previous.proposal_id != proposal_id:
                return self._reject(proposal_id, store_reasons.IDEMPOTENCY_CONFLICT)
            prior_context = self._store.get_execution_context(
                stored.proposal.idempotency_key
            )
            prior_digest = prior_context.get("proposal_digest")
            current_digest = proposal_digest(stored.proposal)
            if (
                current_digest != stored.digest
                or (isinstance(prior_digest, str) and prior_digest != current_digest)
            ):
                return self._reject(proposal_id, store_reasons.IDEMPOTENCY_CONFLICT)
            if previous.outcome is not ActionOutcome.EXECUTION_UNKNOWN:
                return GatewayResult(REPLAYED, attempt=previous)
            return self._reconcile_unknown(stored, approval, previous)

        failure, snapshot = self._preflight(stored, approval)
        if failure is not None:
            return self._reject(proposal_id, failure)
        assert snapshot is not None
        decision = self._store.get_decision(proposal_id)
        if decision is None:
            return self._reject(proposal_id, store_reasons.POLICY_DENIED)

        now = self._clock()
        attempt = ActionAttempt(
            schema_version="0.1",
            attempt_id=self._ids("attempt"),
            proposal_id=proposal_id,
            idempotency_key=stored.proposal.idempotency_key,
            attempt_number=1,
            preflight=ActionPreflightSnapshot(
                schema_version="0.1",
                target=stored.proposal.arguments.target,
                state_fingerprint=snapshot.state_fingerprint,
                observed_at=now,
                proposal_digest=stored.digest,
            ),
            authorization_decision_id=decision.decision.decision_id,
            operation_started_at=now,
            operation_finished_at=now,
            outcome=ActionOutcome.EXECUTION_UNKNOWN,
            before_resource_version=str(snapshot.resource_version),
            after_resource_version=None,
            sanitized_error_class=None,
            trace_id=self._ids("trace"),
        )
        context = self._snapshot_context(snapshot, stored.digest)
        try:
            self._store.claim_attempt(approval.approval.approval_id, attempt, context)
        except StoreError as error:
            if error.code in {
                store_reasons.APPROVAL_CONSUMED,
                store_reasons.DUPLICATE_ATTEMPT_KEY,
            }:
                concurrent = self._store.get_attempt_by_key(
                    stored.proposal.idempotency_key
                )
                if concurrent is not None and concurrent.proposal_id == proposal_id:
                    if concurrent.outcome is ActionOutcome.EXECUTION_UNKNOWN:
                        return GatewayResult(EXECUTION_UNKNOWN, attempt=concurrent)
                    return GatewayResult(REPLAYED, attempt=concurrent)
            return self._reject(proposal_id, self._gateway_code(error.code))
        self._store.update_incident_status(
            stored.proposal.incident_id, IncidentStatus.EXECUTING
        )
        return self._write(stored, attempt, snapshot)

    def _preflight(
        self, stored: StoredProposal, approval: StoredApproval
    ) -> tuple[str | None, SimSnapshot | None]:
        proposal = stored.proposal
        current_approval = approval.approval
        now = self._clock()
        if current_approval.decision is not ApprovalDecision.APPROVE:
            return store_reasons.APPROVAL_REJECTED, None
        if current_approval.revoked:
            return APPROVAL_REVOKED, None
        if current_approval.consumed:
            return store_reasons.APPROVAL_CONSUMED, None
        if now >= current_approval.expires_at:
            return store_reasons.APPROVAL_EXPIRED, None
        if now >= proposal.expires_at:
            return store_reasons.PROPOSAL_EXPIRED, None
        if current_approval.approver_role not in self._policy_config.required_approval_roles:
            return store_reasons.ROLE_NOT_PERMITTED, None
        digest = proposal_digest(proposal)
        if digest != stored.digest or digest != current_approval.proposal_digest:
            return store_reasons.DIGEST_MISMATCH, None
        target_identity = canonical_json(proposal.arguments.target.model_dump(mode="json"))
        if target_identity != approval.target_identity:
            return TARGET_MISMATCH, None
        bundle_digest = policy_bundle_digest(self._policy_config)
        if (
            current_approval.policy_version != self._policy_config.policy_version
            or approval.policy_bundle_digest != bundle_digest
            or proposal.policy_version != self._policy_config.policy_version
            or proposal.policy_bundle_digest != bundle_digest
        ):
            return store_reasons.POLICY_CHANGED, None
        try:
            snapshot = self._source.snapshot()
        except Exception:
            return store_reasons.STATE_UNAVAILABLE, None
        if (
            snapshot.state_fingerprint != current_approval.state_fingerprint
            or snapshot.state_fingerprint != proposal.expected_state_fingerprint
        ):
            return STATE_CHANGED, None
        incident = self._store.get_incident(proposal.incident_id)
        if incident is None:
            return store_reasons.INCIDENT_NOT_FOUND, None
        decision = evaluate(
            self._policy_config,
            PolicyInput(
                proposal_id=proposal.proposal_id,
                action_type=proposal.action_type,
                arguments=proposal.arguments,
                target=proposal.arguments.target,
                scenario_id=incident.scenario_id,
                current_snapshot=snapshot,
                now=now,
                proposal_created_at=proposal.created_at,
                proposal_expires_at=proposal.expires_at,
                environment_id=proposal.environment_id,
                incident_status=incident.status,
                attempts_so_far=self._store.attempt_count(proposal.proposal_id),
            ),
        )
        if decision.decision is PolicyDecisionType.DENY:
            return store_reasons.POLICY_DENIED, None
        if decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL:
            return store_reasons.POLICY_CHANGED, None
        return None, snapshot

    def _write(
        self, stored: StoredProposal, attempt: ActionAttempt, snapshot: SimSnapshot
    ) -> GatewayResult:
        proposal = stored.proposal
        try:
            operation = self._operation(proposal, snapshot)
            result = self._writer.apply(operation)
        except PreconditionFailed:
            failed = attempt.model_copy(
                update={
                    "operation_finished_at": self._clock(),
                    "outcome": ActionOutcome.FAILED,
                    "sanitized_error_class": PRECONDITION_FAILED,
                }
            )
            self._store.finish_attempt(failed, ProposalState.FAILED)
            self._store.update_incident_status(
                proposal.incident_id,
                IncidentStatus.ESCALATED,
                terminal_reason=PRECONDITION_FAILED,
            )
            return GatewayResult(PRECONDITION_FAILED, attempt=failed)
        except (TimeoutError, OSError):
            return self._finish_unknown(stored, attempt, EXECUTION_UNKNOWN)
        except UnsupportedOperation:
            failed = attempt.model_copy(
                update={
                    "operation_finished_at": self._clock(),
                    "outcome": ActionOutcome.FAILED,
                    "sanitized_error_class": "unsupported_operation",
                }
            )
            self._store.finish_attempt(failed, ProposalState.FAILED)
            self._store.update_incident_status(
                proposal.incident_id,
                IncidentStatus.ESCALATED,
                terminal_reason="unsupported_operation",
            )
            return GatewayResult("unsupported_operation", attempt=failed)
        except Exception as error:
            return self._finish_unknown(stored, attempt, type(error).__name__)

        completed = attempt.model_copy(
            update={
                "operation_finished_at": self._clock(),
                "outcome": ActionOutcome.EXECUTED,
                "after_resource_version": str(result.after_resource_version),
                "sanitized_error_class": None,
            }
        )
        self._store.finish_attempt(completed, ProposalState.EXECUTED)
        self._store.update_incident_status(proposal.incident_id, IncidentStatus.VERIFYING)
        return GatewayResult("executed", attempt=completed, write_result=result)

    def _finish_unknown(
        self, stored: StoredProposal, attempt: ActionAttempt, error_class: str
    ) -> GatewayResult:
        unknown = attempt.model_copy(
            update={
                "operation_finished_at": self._clock(),
                "outcome": ActionOutcome.EXECUTION_UNKNOWN,
                "sanitized_error_class": error_class[:80],
            }
        )
        self._store.finish_attempt(unknown, ProposalState.EXECUTION_UNKNOWN)
        self._store.update_incident_status(
            stored.proposal.incident_id,
            IncidentStatus.ESCALATED,
            terminal_reason=EXECUTION_UNKNOWN,
        )
        return GatewayResult(EXECUTION_UNKNOWN, attempt=unknown)

    def _reconcile_unknown(
        self, stored: StoredProposal, approval: StoredApproval, attempt: ActionAttempt
    ) -> GatewayResult:
        try:
            snapshot = self._source.snapshot()
        except Exception:
            return GatewayResult(store_reasons.STATE_UNAVAILABLE, attempt=attempt)
        before = int(attempt.before_resource_version or "0")
        if snapshot.resource_version > before:
            if self._effect_observed(stored, attempt, snapshot):
                completed = attempt.model_copy(
                    update={
                        "operation_finished_at": self._clock(),
                        "outcome": ActionOutcome.EXECUTED,
                        "after_resource_version": str(snapshot.resource_version),
                        "sanitized_error_class": None,
                    }
                )
                self._store.finish_attempt(completed, ProposalState.EXECUTED)
                self._store.update_incident_status(
                    stored.proposal.incident_id, IncidentStatus.VERIFYING
                )
                return GatewayResult("reconciled", attempt=completed)
            return GatewayResult(EXECUTION_UNKNOWN, attempt=attempt)
        if snapshot.resource_version != before:
            return GatewayResult(EXECUTION_UNKNOWN, attempt=attempt)
        if attempt.attempt_number >= stored.proposal.attempt_limit:
            return GatewayResult(EXECUTION_UNKNOWN, attempt=attempt)
        retry_failure = self._retry_preflight(stored, approval, snapshot)
        if retry_failure is not None:
            return self._reject(stored.proposal.proposal_id, retry_failure)
        now = self._clock()
        retry = attempt.model_copy(
            update={
                "attempt_number": attempt.attempt_number + 1,
                "preflight": attempt.preflight.model_copy(
                    update={
                        "state_fingerprint": snapshot.state_fingerprint,
                        "observed_at": now,
                    }
                ),
                "operation_started_at": now,
                "operation_finished_at": now,
                "before_resource_version": str(snapshot.resource_version),
                "after_resource_version": None,
                "sanitized_error_class": None,
            }
        )
        try:
            retry = self._store.claim_retry(retry)
        except StoreError as error:
            return self._reject(
                stored.proposal.proposal_id, self._gateway_code(error.code)
            )
        return self._write(stored, retry, snapshot)

    def _retry_preflight(
        self, stored: StoredProposal, approval: StoredApproval, snapshot: SimSnapshot
    ) -> str | None:
        proposal = stored.proposal
        current = approval.approval
        now = self._clock()
        if current.revoked:
            return APPROVAL_REVOKED
        if current.decision is not ApprovalDecision.APPROVE:
            return store_reasons.APPROVAL_REJECTED
        if now >= current.expires_at:
            return store_reasons.APPROVAL_EXPIRED
        if now >= proposal.expires_at:
            return store_reasons.PROPOSAL_EXPIRED
        if current.approver_role not in self._policy_config.required_approval_roles:
            return store_reasons.ROLE_NOT_PERMITTED
        bundle_digest = policy_bundle_digest(self._policy_config)
        if (
            current.policy_version != self._policy_config.policy_version
            or approval.policy_bundle_digest != bundle_digest
            or proposal.policy_version != self._policy_config.policy_version
            or proposal.policy_bundle_digest != bundle_digest
        ):
            return store_reasons.POLICY_CHANGED
        if snapshot.state_fingerprint != current.state_fingerprint:
            return STATE_CHANGED
        incident = self._store.get_incident(proposal.incident_id)
        if incident is None:
            return store_reasons.INCIDENT_NOT_FOUND
        decision = evaluate(
            self._policy_config,
            PolicyInput(
                proposal_id=proposal.proposal_id,
                action_type=proposal.action_type,
                arguments=proposal.arguments,
                target=proposal.arguments.target,
                scenario_id=incident.scenario_id,
                current_snapshot=snapshot,
                now=now,
                proposal_created_at=proposal.created_at,
                proposal_expires_at=proposal.expires_at,
                environment_id=proposal.environment_id,
                incident_status=incident.status,
                attempts_so_far=self._store.attempt_count(proposal.proposal_id),
            ),
        )
        return (
            store_reasons.POLICY_DENIED
            if decision.decision is PolicyDecisionType.DENY
            else None
        )

    def _effect_observed(
        self, stored: StoredProposal, attempt: ActionAttempt, snapshot: SimSnapshot
    ) -> bool:
        arguments = stored.proposal.arguments
        if isinstance(arguments, ScaleDeploymentArgs):
            return snapshot.observation.desired_replicas == arguments.replicas
        if isinstance(arguments, RestartDeploymentArgs):
            context = self._store.get_execution_context(attempt.idempotency_key)
            before_revision = context.get("revision")
            return (
                isinstance(before_revision, int)
                and not isinstance(before_revision, bool)
                and snapshot.revision > before_revision
                and snapshot.observation.desired_replicas == context.get("desired_replicas")
                and snapshot.readiness_path == context.get("readiness_path")
                and str(snapshot.readiness_port) == str(context.get("readiness_port"))
            )
        return False

    def _operation(self, proposal: ActionProposal, snapshot: SimSnapshot) -> WriteOperation:
        arguments = proposal.arguments
        if isinstance(arguments, RestartDeploymentArgs):
            return RestartOperation(snapshot.resource_version)
        if isinstance(arguments, ScaleDeploymentArgs):
            return ScaleOperation(arguments.replicas, snapshot.resource_version)
        if isinstance(arguments, RollbackDeploymentArgs):
            return RollbackOperation(arguments.revision, snapshot.resource_version)
        raise UnsupportedOperation("unknown action arguments")

    @staticmethod
    def _snapshot_context(
        snapshot: SimSnapshot, proposal_digest_value: str
    ) -> dict[str, JsonValue]:
        return {
            "proposal_digest": proposal_digest_value,
            "revision": snapshot.revision,
            "desired_replicas": snapshot.observation.desired_replicas,
            "readiness_path": snapshot.readiness_path,
            "readiness_port": str(snapshot.readiness_port),
            "tick": snapshot.tick,
        }

    def _reject(self, proposal_id: str, reason_code: str) -> GatewayResult:
        self._store.record_gateway_rejection(proposal_id, reason_code)
        return GatewayResult(reason_code)

    @staticmethod
    def _gateway_code(code: str) -> str:
        if code == store_reasons.ALREADY_REVOKED:
            return APPROVAL_REVOKED
        if code == store_reasons.STATE_CHANGED_SINCE_PROPOSAL:
            return STATE_CHANGED
        return code


__all__ = [
    "APPROVAL_REVOKED",
    "EXECUTION_UNKNOWN",
    "Gateway",
    "GatewayResult",
    "PRECONDITION_FAILED",
    "PRINCIPAL_MISMATCH",
    "REPLAYED",
    "STATE_CHANGED",
    "TARGET_MISMATCH",
]
