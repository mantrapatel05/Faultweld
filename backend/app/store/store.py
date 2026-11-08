"""SQLite persistence for incidents, proposals, decisions, approvals and attempts.

The store records what trusted code decided. It is not the authority boundary: the gateway
re-checks everything before a mutation. Its job is to make every record durable, ordered and
tamper-evident enough that an approval can only ever authorize the proposal it was shown.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

from pydantic import JsonValue

from app.agent.identity import ENVIRONMENT_ID
from app.agent.ids import IdFactory, uuid_id
from app.agent.proposal import proposal_digest
from app.domain.schemas import (
    ActionAttempt,
    ActionOutcome,
    ActionProposal,
    Approval,
    ApprovalDecision,
    Incident,
    IncidentStatus,
    PolicyDecision,
    PolicyDecisionType,
)
from app.evidence.canonical import canonical_json
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.policy.principals import Principal, PrincipalKind, known_principal
from app.sim.cluster import SimError, SimSnapshot, SimState
from app.store import reasons
from app.store.models import (
    TRANSITIONS,
    ApprovalOutcome,
    AuditEvent,
    ProposalState,
    StoredApproval,
    StoredDecision,
    StoredProposal,
    StoreError,
    state_after_decision,
)

DEFAULT_ENVIRONMENT_ID = ENVIRONMENT_ID
MAX_REJECT_REASON_LENGTH = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    json TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
);

CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    proposal_digest TEXT NOT NULL,
    approver_principal_id TEXT NOT NULL,
    approver_role TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_bundle_digest TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    json TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    before_resource_version TEXT,
    after_resource_version TEXT,
    outcome TEXT NOT NULL,
    error_class TEXT,
    json TEXT NOT NULL,
    execution_context TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TABLE IF NOT EXISTS environments (
    environment_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_resolve_principal(principal_id: str) -> Principal:
    principal = known_principal(principal_id)
    if principal is None:
        raise StoreError(reasons.UNKNOWN_PRINCIPAL, f"unknown principal {principal_id!r}")
    return principal


def _require_reason(reason: str) -> str:
    cleaned = reason.strip()
    if not cleaned or len(cleaned) > MAX_REJECT_REASON_LENGTH:
        raise StoreError(
            reasons.INVALID_REASON,
            f"reason must be 1-{MAX_REJECT_REASON_LENGTH} characters",
        )
    return cleaned


class Store:
    """One SQLite database. Construct, use, and ``close``; the file is safe to reopen."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        policy_config: PolicyConfig | None = None,
        snapshot: Callable[[], SimSnapshot] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        ids: IdFactory = uuid_id,
        resolve_principal: Callable[[str], Principal] | None = None,
    ) -> None:
        self._policy_config = policy_config or PolicyConfig()
        self._snapshot = snapshot
        self._clock = clock
        self._ids = ids
        self._resolve_principal = resolve_principal or _default_resolve_principal
        self._connection: sqlite3.Connection | None = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    # -- lifecycle -------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create every table and trigger. Safe to call repeatedly on the same file."""
        connection = self._conn()
        connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if "execution_context" not in columns:
            connection.execute(
                "ALTER TABLE attempts ADD COLUMN execution_context TEXT NOT NULL DEFAULT '{}'"
            )
        connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("store is closed")
        return self._connection

    # -- incidents -------------------------------------------------------------------

    def create_incident(self, incident: Incident) -> None:
        payload = canonical_json(incident.model_dump(mode="json"))
        with self._conn():
            try:
                self._conn().execute(
                    """
                    INSERT INTO incidents (incident_id, status, json, version, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (
                        incident.incident_id,
                        incident.status.value,
                        payload,
                        incident.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreError(
                    reasons.DUPLICATE_INCIDENT,
                    f"incident {incident.incident_id!r} already exists",
                ) from error
            self._audit("incident_created", incident.incident_id, {"status": incident.status.value})

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self._conn().execute(
            "SELECT json FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            return None
        return Incident.model_validate_json(row["json"])

    def update_incident_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        *,
        terminal_reason: str | None = None,
    ) -> Incident:
        now = self._clock()
        with self._conn():
            row = self._conn().execute(
                "SELECT json, version FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise StoreError(
                    reasons.INCIDENT_NOT_FOUND, f"incident {incident_id!r} does not exist"
                )
            current = Incident.model_validate_json(row["json"])
            payload = current.model_dump(mode="json")
            payload.update(
                {
                    "status": status.value,
                    "updated_at": now.isoformat(),
                    "terminal_reason": terminal_reason,
                }
            )
            updated = Incident.model_validate(payload)
            self._conn().execute(
                """
                UPDATE incidents
                SET status = ?, json = ?, version = version + 1, updated_at = ?
                WHERE incident_id = ?
                """,
                (status.value, canonical_json(payload), now.isoformat(), incident_id),
            )
            self._audit(
                "incident_status_changed",
                incident_id,
                {"status": status.value, "previous_status": current.status.value},
            )
            return updated

    # -- environments ----------------------------------------------------------------

    def save_environment(self, environment_id: str, state: SimState) -> None:
        now = self._clock()
        payload = canonical_json(asdict(state))
        with self._conn():
            self._conn().execute(
                """
                INSERT INTO environments (environment_id, state, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(environment_id) DO UPDATE
                SET state = excluded.state, updated_at = excluded.updated_at
                """,
                (environment_id, payload, now.isoformat()),
            )

    def load_environment(self, environment_id: str) -> SimState | None:
        row = self._conn().execute(
            "SELECT state FROM environments WHERE environment_id = ?", (environment_id,)
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["state"])
        if not isinstance(decoded, dict):
            raise StoreError(
                reasons.INVALID_ENVIRONMENT,
                f"environment {environment_id!r} does not contain a state object",
            )
        try:
            return SimState.from_mapping(decoded)
        except SimError as error:
            raise StoreError(reasons.INVALID_ENVIRONMENT, str(error)) from error

    # -- proposals and decisions -----------------------------------------------------

    def record_proposal(
        self, proposal: ActionProposal, decision: PolicyDecision
    ) -> StoredProposal:
        if decision.proposal_id != proposal.proposal_id:
            raise StoreError(
                reasons.PROPOSAL_MISMATCH,
                "the policy decision is for a different proposal",
            )
        digest = proposal_digest(proposal)
        now = self._clock()
        target = state_after_decision(decision.decision)
        with self._conn():
            if self._proposal_row(proposal.proposal_id) is not None:
                raise StoreError(
                    reasons.DUPLICATE_PROPOSAL,
                    f"proposal {proposal.proposal_id!r} already exists",
                )
            try:
                self._conn().execute(
                    """
                    INSERT INTO proposals
                        (proposal_id, incident_id, digest, json, state, version)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.incident_id,
                        digest,
                        canonical_json(proposal.model_dump(mode="json")),
                        ProposalState.PREFLIGHTED.value,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreError(
                    reasons.INCIDENT_NOT_FOUND,
                    f"proposal {proposal.proposal_id!r} references an unknown incident",
                ) from error
            try:
                self._conn().execute(
                    """
                    INSERT INTO policy_decisions
                        (decision_id, proposal_id, decision, json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.proposal_id,
                        decision.decision.value,
                        canonical_json(decision.model_dump(mode="json")),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreError(
                    reasons.PROPOSAL_NOT_FOUND,
                    "the policy decision references an unknown proposal",
                ) from error
            self._audit(
                "proposal_recorded",
                proposal.proposal_id,
                {
                    "decision": decision.decision.value,
                    "reason_codes": list(decision.reason_codes),
                },
            )
            stored = StoredProposal(
                proposal=proposal,
                digest=digest,
                state=ProposalState.PREFLIGHTED,
                version=1,
            )
            return self._transition_locked(stored, target)

    def get_proposal(self, proposal_id: str) -> StoredProposal | None:
        row = self._proposal_row(proposal_id)
        return None if row is None else self._stored_proposal(row)

    def get_latest_proposal(self) -> StoredProposal | None:
        row = self._conn().execute(
            "SELECT proposal_id, incident_id, digest, json, state, version "
            "FROM proposals ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._stored_proposal(row)

    def get_decision(self, proposal_id: str) -> StoredDecision | None:
        row = self._conn().execute(
            """
            SELECT json FROM policy_decisions
            WHERE proposal_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredDecision(decision=PolicyDecision.model_validate_json(row["json"]))

    def transition(
        self, proposal_id: str, target: ProposalState, expected_version: int
    ) -> StoredProposal:
        with self._conn():
            row = self._proposal_row(proposal_id)
            if row is None:
                raise StoreError(
                    reasons.PROPOSAL_NOT_FOUND, f"proposal {proposal_id!r} does not exist"
                )
            stored = self._stored_proposal(row)
            if stored.version != expected_version:
                raise StoreError(
                    reasons.STALE_VERSION,
                    f"expected version {expected_version}, found {stored.version}",
                )
            return self._transition_locked(stored, ProposalState(target))

    # -- approvals -------------------------------------------------------------------

    def approve(
        self, proposal_id: str, expected_version: int, principal_id: str
    ) -> ApprovalOutcome:
        return self._decide(
            proposal_id=proposal_id,
            expected_version=expected_version,
            principal_id=principal_id,
            decision=ApprovalDecision.APPROVE,
            reason=None,
        )

    def reject(
        self,
        proposal_id: str,
        expected_version: int,
        principal_id: str,
        *,
        reason: str,
    ) -> ApprovalOutcome:
        return self._decide(
            proposal_id=proposal_id,
            expected_version=expected_version,
            principal_id=principal_id,
            decision=ApprovalDecision.REJECT,
            reason=_require_reason(reason),
        )

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        row = self._approval_row(approval_id)
        return None if row is None else self._stored_approval(row)

    def get_approval_for_proposal(self, proposal_id: str) -> StoredApproval | None:
        row = self._approval_row_for_proposal(proposal_id)
        return None if row is None else self._stored_approval(row)

    def revoke(self, approval_id: str, principal_id: str) -> StoredApproval:
        with self._conn():
            row = self._approval_row(approval_id)
            if row is None:
                raise StoreError(
                    reasons.APPROVAL_NOT_FOUND, f"approval {approval_id!r} does not exist"
                )
            stored = self._stored_approval(row)
            principal = self._resolve(principal_id)
            if principal.kind is PrincipalKind.AGENT:
                raise StoreError(reasons.SELF_APPROVAL, "an agent cannot revoke an approval")
            if principal.role not in self._policy_config.required_approval_roles:
                raise StoreError(
                    reasons.ROLE_NOT_PERMITTED,
                    f"role {principal.role!r} may not revoke approvals",
                )
            if stored.approval.revoked:
                raise StoreError(
                    reasons.ALREADY_REVOKED, f"approval {approval_id!r} is already revoked"
                )
            revoked = stored.approval.model_copy(update={"revoked": True})
            updated = self._conn().execute(
                """
                UPDATE approvals
                SET revoked = 1, version = version + 1, json = ?
                WHERE approval_id = ? AND version = ?
                """,
                (canonical_json(revoked.model_dump(mode="json")), approval_id, stored.version),
            )
            if updated.rowcount != 1:
                raise StoreError(
                    reasons.STALE_VERSION, "the approval changed while it was being revoked"
                )
            self._audit(
                "approval_revoked",
                approval_id,
                {"proposal_id": stored.approval.proposal_id},
            )
            return StoredApproval(
                approval=revoked,
                version=stored.version + 1,
                policy_bundle_digest=stored.policy_bundle_digest,
                environment_id=stored.environment_id,
                incident_id=stored.incident_id,
                target_identity=stored.target_identity,
            )

    def _decide(
        self,
        *,
        proposal_id: str,
        expected_version: int,
        principal_id: str,
        decision: ApprovalDecision,
        reason: str | None,
    ) -> ApprovalOutcome:
        now = self._clock()
        with self._conn():
            row = self._proposal_row(proposal_id)
            if row is None:
                raise StoreError(
                    reasons.PROPOSAL_NOT_FOUND, f"proposal {proposal_id!r} does not exist"
                )
            stored = self._stored_proposal(row)

            existing_row = self._approval_row_for_proposal(proposal_id)
            if existing_row is not None:
                existing = self._stored_approval(existing_row)
                same = (
                    existing.approval.decision is decision
                    and existing.approval.approver_principal_id == principal_id
                    and existing.approval.proposal_digest == stored.digest
                )
                if same:
                    return ApprovalOutcome(approval=existing, already_decided=True)
                raise StoreError(
                    reasons.CONFLICTING_DECISION,
                    f"proposal {proposal_id!r} already has a "
                    f"{existing.approval.decision.value} decision",
                )

            if stored.state is not ProposalState.AWAITING_APPROVAL:
                raise StoreError(
                    reasons.PROPOSAL_NOT_AWAITING_APPROVAL,
                    f"proposal is {stored.state.value}, not awaiting_approval",
                )
            if now >= stored.proposal.expires_at:
                raise StoreError(
                    reasons.PROPOSAL_EXPIRED, f"proposal {proposal_id!r} has expired"
                )
            if (
                stored.proposal.proposal_id != proposal_id
                or proposal_digest(stored.proposal) != stored.digest
            ):
                raise StoreError(
                    reasons.DIGEST_MISMATCH,
                    "the stored proposal does not match its recorded digest",
                )
            if stored.version != expected_version:
                raise StoreError(
                    reasons.STALE_VERSION,
                    f"expected version {expected_version}, found {stored.version}",
                )

            principal = self._resolve(principal_id)
            if principal.kind is PrincipalKind.AGENT:
                raise StoreError(reasons.SELF_APPROVAL, "an agent cannot decide its own proposal")
            config = self._policy_config
            if principal.role not in config.required_approval_roles:
                raise StoreError(
                    reasons.ROLE_NOT_PERMITTED,
                    f"role {principal.role!r} may not decide proposals",
                )

            snapshot = self._fresh_snapshot()
            if snapshot.state_fingerprint != stored.proposal.expected_state_fingerprint:
                raise StoreError(
                    reasons.STATE_CHANGED_SINCE_PROPOSAL,
                    "the live state no longer matches what the proposal was based on",
                )
            bundle_digest = policy_bundle_digest(config)
            if (
                stored.proposal.policy_version != config.policy_version
                or stored.proposal.policy_bundle_digest != bundle_digest
            ):
                raise StoreError(
                    reasons.POLICY_CHANGED,
                    "the policy bundle changed after the proposal was created",
                )

            incident = self.get_incident(stored.proposal.incident_id)
            if incident is None:
                raise StoreError(
                    reasons.INCIDENT_NOT_FOUND,
                    f"incident {stored.proposal.incident_id!r} does not exist",
                )
            policy_input = PolicyInput(
                proposal_id=stored.proposal.proposal_id,
                action_type=stored.proposal.action_type,
                arguments=stored.proposal.arguments,
                target=stored.proposal.arguments.target,
                scenario_id=incident.scenario_id,
                current_snapshot=snapshot,
                now=now,
                proposal_created_at=stored.proposal.created_at,
                proposal_expires_at=stored.proposal.expires_at,
                environment_id=stored.proposal.environment_id,
                incident_status=incident.status,
                attempts_so_far=self._attempt_count(proposal_id),
            )
            current = evaluate(config, policy_input)
            if current.decision is PolicyDecisionType.DENY:
                raise StoreError(
                    reasons.POLICY_DENIED,
                    "current policy denies: " + ", ".join(current.reason_codes),
                )
            if current.decision is not PolicyDecisionType.REQUIRE_APPROVAL:
                raise StoreError(
                    reasons.POLICY_CHANGED,
                    "current policy no longer requires approval for this proposal",
                )

            approval_id = self._ids("approval")
            approval = Approval(
                schema_version="0.1",
                approval_id=approval_id,
                proposal_id=proposal_id,
                proposal_digest=stored.digest,
                approver_principal_id=principal.principal_id,
                approver_role=principal.role,
                decision=decision,
                decided_at=now,
                expires_at=min(
                    now + timedelta(seconds=config.approval_ttl_seconds),
                    stored.proposal.expires_at,
                ),
                policy_version=stored.proposal.policy_version,
                state_fingerprint=snapshot.state_fingerprint,
                consumed=False,
                revoked=False,
            )
            target_identity = canonical_json(
                stored.proposal.arguments.target.model_dump(mode="json")
            )
            self._conn().execute(
                """
                INSERT INTO approvals (
                    approval_id, proposal_id, proposal_digest,
                    approver_principal_id, approver_role, decision,
                    decided_at, expires_at, policy_version, policy_bundle_digest,
                    environment_id, incident_id, target_identity, state_fingerprint,
                    consumed, revoked, version, json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?)
                """,
                (
                    approval_id,
                    proposal_id,
                    stored.digest,
                    principal.principal_id,
                    principal.role,
                    decision.value,
                    now.isoformat(),
                    approval.expires_at.isoformat(),
                    stored.proposal.policy_version,
                    stored.proposal.policy_bundle_digest,
                    stored.proposal.environment_id,
                    stored.proposal.incident_id,
                    target_identity,
                    snapshot.state_fingerprint,
                    canonical_json(approval.model_dump(mode="json")),
                ),
            )
            target_state = (
                ProposalState.AUTHORIZED
                if decision is ApprovalDecision.APPROVE
                else ProposalState.DENIED
            )
            self._transition_locked(stored, target_state)
            detail: dict[str, JsonValue] = {
                "proposal_id": proposal_id,
                "decision": decision.value,
            }
            if reason is not None:
                detail["reason"] = reason
            self._audit("approval_recorded", approval_id, detail)
            return ApprovalOutcome(
                approval=StoredApproval(
                    approval=approval,
                    version=1,
                    policy_bundle_digest=stored.proposal.policy_bundle_digest,
                    environment_id=stored.proposal.environment_id,
                    incident_id=stored.proposal.incident_id,
                    target_identity=target_identity,
                ),
                already_decided=False,
            )

    # -- attempts --------------------------------------------------------------------

    def insert_attempt(self, attempt: ActionAttempt) -> None:
        payload = canonical_json(attempt.model_dump(mode="json"))
        with self._conn():
            try:
                self._conn().execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, proposal_id, idempotency_key, attempt_number,
                        status, before_resource_version, after_resource_version,
                        outcome, error_class, json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.proposal_id,
                        attempt.idempotency_key,
                        attempt.attempt_number,
                        attempt.outcome.value,
                        attempt.before_resource_version,
                        attempt.after_resource_version,
                        attempt.outcome.value,
                        attempt.sanitized_error_class,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "idempotency_key" in str(error):
                    raise StoreError(
                        reasons.DUPLICATE_ATTEMPT_KEY,
                        f"attempt key {attempt.idempotency_key!r} was already used",
                    ) from error
                raise StoreError(
                    reasons.PROPOSAL_NOT_FOUND,
                    f"attempt references unknown proposal {attempt.proposal_id!r}",
                ) from error
            self._audit(
                "attempt_recorded",
                attempt.attempt_id,
                {"proposal_id": attempt.proposal_id, "outcome": attempt.outcome.value},
            )

    def record_gateway_rejection(self, proposal_id: str, reason_code: str) -> None:
        with self._conn():
            self._audit(
                "gateway_rejected",
                proposal_id,
                {"reason_code": reason_code},
            )

    def claim_attempt(
        self,
        approval_id: str,
        attempt: ActionAttempt,
        execution_context: Mapping[str, JsonValue],
    ) -> None:
        """Atomically consume approval, claim its idempotency key and start execution."""
        connection = self._conn()
        connection.execute("BEGIN IMMEDIATE")
        try:
            approval_row = self._approval_row(approval_id)
            if approval_row is None:
                raise StoreError(reasons.APPROVAL_NOT_FOUND, "approval disappeared")
            approval = self._stored_approval(approval_row).approval
            if approval.revoked:
                raise StoreError(reasons.ALREADY_REVOKED, "approval was revoked")
            if approval.decision is not ApprovalDecision.APPROVE:
                raise StoreError(reasons.APPROVAL_REJECTED, "approval records a rejection")
            if approval.consumed:
                raise StoreError(reasons.APPROVAL_CONSUMED, "approval was already consumed")
            if self._now() >= approval.expires_at:
                raise StoreError(reasons.APPROVAL_EXPIRED, "approval has expired")
            if approval.approver_role not in self._policy_config.required_approval_roles:
                raise StoreError(reasons.ROLE_NOT_PERMITTED, "approver role is no longer permitted")

            proposal_row = self._proposal_row(attempt.proposal_id)
            if proposal_row is None:
                raise StoreError(reasons.PROPOSAL_NOT_FOUND, "proposal disappeared")
            stored = self._stored_proposal(proposal_row)
            if stored.state is not ProposalState.AUTHORIZED:
                raise StoreError(
                    reasons.ILLEGAL_TRANSITION,
                    f"proposal is {stored.state.value}, not authorized",
                )
            claim_now = self._now()
            if claim_now >= stored.proposal.expires_at:
                raise StoreError(reasons.PROPOSAL_EXPIRED, "proposal expired before claim")
            current_bundle = policy_bundle_digest(self._policy_config)
            if (
                stored.proposal.policy_version != self._policy_config.policy_version
                or stored.proposal.policy_bundle_digest != current_bundle
                or approval.policy_version != self._policy_config.policy_version
                or str(approval_row["policy_bundle_digest"]) != current_bundle
            ):
                raise StoreError(reasons.POLICY_CHANGED, "policy changed before claim")
            if (
                proposal_digest(stored.proposal) != stored.digest
                or approval.proposal_digest != stored.digest
                or attempt.preflight.proposal_digest != stored.digest
            ):
                raise StoreError(reasons.DIGEST_MISMATCH, "proposal digest changed before claim")
            target_identity = canonical_json(
                stored.proposal.arguments.target.model_dump(mode="json")
            )
            if approval.proposal_id != stored.proposal.proposal_id or target_identity != str(
                approval_row["target_identity"]
            ):
                raise StoreError(reasons.DIGEST_MISMATCH, "approval target no longer matches")
            snapshot = self._fresh_snapshot()
            if (
                snapshot.state_fingerprint != approval.state_fingerprint
                or snapshot.state_fingerprint != attempt.preflight.state_fingerprint
            ):
                raise StoreError(
                    reasons.STATE_CHANGED_SINCE_PROPOSAL,
                    "live state changed before the execution claim",
                )
            incident = self.get_incident(stored.proposal.incident_id)
            if incident is None:
                raise StoreError(reasons.INCIDENT_NOT_FOUND, "incident disappeared before claim")
            current_decision = evaluate(
                self._policy_config,
                PolicyInput(
                    proposal_id=stored.proposal.proposal_id,
                    action_type=stored.proposal.action_type,
                    arguments=stored.proposal.arguments,
                    target=stored.proposal.arguments.target,
                    scenario_id=incident.scenario_id,
                    current_snapshot=snapshot,
                    now=claim_now,
                    proposal_created_at=stored.proposal.created_at,
                    proposal_expires_at=stored.proposal.expires_at,
                    environment_id=stored.proposal.environment_id,
                    incident_status=incident.status,
                    attempts_so_far=self._attempt_count(stored.proposal.proposal_id),
                ),
            )
            if current_decision.decision is PolicyDecisionType.DENY:
                raise StoreError(reasons.POLICY_DENIED, "policy denied before claim")
            if current_decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL:
                raise StoreError(reasons.POLICY_CHANGED, "approval is no longer required")
            if approval.proposal_id != attempt.proposal_id:
                raise StoreError(
                    reasons.APPROVAL_NOT_FOUND, "approval belongs to another proposal"
                )
            if attempt.idempotency_key != stored.proposal.idempotency_key:
                raise StoreError(
                    reasons.IDEMPOTENCY_CONFLICT, "attempt key does not match proposal"
                )

            updated = connection.execute(
                "UPDATE approvals SET consumed = 1, version = version + 1 "
                "WHERE approval_id = ? AND consumed = 0 AND revoked = 0",
                (approval_id,),
            )
            if updated.rowcount != 1:
                raise StoreError(reasons.APPROVAL_CONSUMED, "approval was claimed concurrently")
            self._insert_attempt_locked(attempt, execution_context)
            self._transition_locked(stored, ProposalState.EXECUTING)
            self._audit(
                "execution_claimed",
                attempt.attempt_id,
                {"proposal_id": attempt.proposal_id, "approval_id": approval_id},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def claim_retry(self, retry: ActionAttempt) -> ActionAttempt:
        """Reclaim an unchanged UNKNOWN attempt once if its proposal permits another try."""
        connection = self._conn()
        connection.execute("BEGIN IMMEDIATE")
        try:
            attempt_row = connection.execute(
                "SELECT json FROM attempts WHERE idempotency_key = ?",
                (retry.idempotency_key,),
            ).fetchone()
            if attempt_row is None:
                raise StoreError(reasons.DUPLICATE_ATTEMPT_KEY, "unknown attempt key")
            attempt = ActionAttempt.model_validate_json(attempt_row["json"])
            proposal_row = self._proposal_row(attempt.proposal_id)
            if proposal_row is None:
                raise StoreError(reasons.PROPOSAL_NOT_FOUND, "proposal disappeared")
            stored = self._stored_proposal(proposal_row)
            if (
                attempt.outcome is not ActionOutcome.EXECUTION_UNKNOWN
                or stored.state is not ProposalState.EXECUTION_UNKNOWN
            ):
                raise StoreError(reasons.DUPLICATE_ATTEMPT_KEY, "attempt is not recoverable")
            approval_row = self._approval_row_for_proposal(stored.proposal.proposal_id)
            if approval_row is None:
                raise StoreError(reasons.APPROVAL_NOT_FOUND, "approval disappeared")
            approval = self._stored_approval(approval_row).approval
            if approval.revoked:
                raise StoreError(reasons.ALREADY_REVOKED, "approval was revoked")
            if approval.decision is not ApprovalDecision.APPROVE:
                raise StoreError(reasons.APPROVAL_REJECTED, "approval records a rejection")
            if not approval.consumed:
                raise StoreError(reasons.APPROVAL_NOT_FOUND, "approval is no longer claimed")
            now = self._now()
            if now >= approval.expires_at or now >= stored.proposal.expires_at:
                raise StoreError(reasons.APPROVAL_EXPIRED, "approval expired before retry")
            if approval.approver_role not in self._policy_config.required_approval_roles:
                raise StoreError(reasons.ROLE_NOT_PERMITTED, "approval role is no longer permitted")
            bundle_digest = policy_bundle_digest(self._policy_config)
            if (
                approval.policy_version != self._policy_config.policy_version
                or str(approval_row["policy_bundle_digest"]) != bundle_digest
                or stored.proposal.policy_version != self._policy_config.policy_version
                or stored.proposal.policy_bundle_digest != bundle_digest
            ):
                raise StoreError(reasons.POLICY_CHANGED, "policy changed before retry")
            if (
                proposal_digest(stored.proposal) != stored.digest
                or approval.proposal_digest != stored.digest
                or retry.preflight.proposal_digest != stored.digest
            ):
                raise StoreError(reasons.DIGEST_MISMATCH, "proposal digest changed before retry")
            snapshot = self._fresh_snapshot()
            if (
                snapshot.state_fingerprint != approval.state_fingerprint
                or snapshot.state_fingerprint != retry.preflight.state_fingerprint
            ):
                raise StoreError(
                    reasons.STATE_CHANGED_SINCE_PROPOSAL,
                    "live state changed before retry claim",
                )
            incident = self.get_incident(stored.proposal.incident_id)
            if incident is None:
                raise StoreError(reasons.INCIDENT_NOT_FOUND, "incident disappeared before retry")
            decision = evaluate(
                self._policy_config,
                PolicyInput(
                    proposal_id=stored.proposal.proposal_id,
                    action_type=stored.proposal.action_type,
                    arguments=stored.proposal.arguments,
                    target=stored.proposal.arguments.target,
                    scenario_id=incident.scenario_id,
                    current_snapshot=snapshot,
                    now=now,
                    proposal_created_at=stored.proposal.created_at,
                    proposal_expires_at=stored.proposal.expires_at,
                    environment_id=stored.proposal.environment_id,
                    incident_status=incident.status,
                    attempts_so_far=self._attempt_count(stored.proposal.proposal_id),
                ),
            )
            if decision.decision is PolicyDecisionType.DENY:
                raise StoreError(reasons.POLICY_DENIED, "policy denied before retry")
            if decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL:
                raise StoreError(reasons.POLICY_CHANGED, "approval is no longer required")
            if attempt.attempt_number >= stored.proposal.attempt_limit:
                raise StoreError(reasons.ATTEMPT_LIMIT_REACHED, "attempt limit is exhausted")
            if retry.attempt_number != attempt.attempt_number + 1:
                raise StoreError(reasons.STALE_VERSION, "retry attempt number is stale")
            self._update_attempt_locked(retry)
            self._transition_locked(stored, ProposalState.EXECUTING)
            self._audit(
                "execution_retry_claimed",
                retry.attempt_id,
                {"attempt": retry.attempt_number},
            )
            connection.commit()
            return retry
        except Exception:
            connection.rollback()
            raise

    def finish_attempt(
        self, attempt: ActionAttempt, target_state: ProposalState
    ) -> None:
        """Persist an outcome and its lifecycle transition in one transaction."""
        connection = self._conn()
        with connection:
            self._update_attempt_locked(attempt)
            row = self._proposal_row(attempt.proposal_id)
            if row is None:
                raise StoreError(reasons.PROPOSAL_NOT_FOUND, "proposal disappeared")
            self._transition_locked(self._stored_proposal(row), target_state)
            self._audit(
                "attempt_finished",
                attempt.attempt_id,
                {"outcome": attempt.outcome.value, "state": target_state.value},
            )

    def get_execution_context(self, idempotency_key: str) -> dict[str, JsonValue]:
        row = self._conn().execute(
            "SELECT execution_context FROM attempts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["execution_context"])
        except (TypeError, ValueError) as error:
            raise StoreError(reasons.CORRUPT_ROW, "attempt context is not valid JSON") from error
        if not isinstance(value, dict):
            raise StoreError(reasons.CORRUPT_ROW, "attempt context is not an object")
        return value

    def _insert_attempt_locked(
        self, attempt: ActionAttempt, execution_context: Mapping[str, JsonValue]
    ) -> None:
        payload = canonical_json(attempt.model_dump(mode="json"))
        try:
            self._conn().execute(
                """INSERT INTO attempts (
                    attempt_id, proposal_id, idempotency_key, attempt_number,
                    status, before_resource_version, after_resource_version,
                    outcome, error_class, json, execution_context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt.attempt_id,
                    attempt.proposal_id,
                    attempt.idempotency_key,
                    attempt.attempt_number,
                    attempt.outcome.value,
                    attempt.before_resource_version,
                    attempt.after_resource_version,
                    attempt.outcome.value,
                    attempt.sanitized_error_class,
                    payload,
                    canonical_json(execution_context),
                ),
            )
        except sqlite3.IntegrityError as error:
            if "idempotency_key" in str(error):
                raise StoreError(
                    reasons.DUPLICATE_ATTEMPT_KEY,
                    f"attempt key {attempt.idempotency_key!r} was already used",
                ) from error
            raise
        self._audit(
            "attempt_recorded",
            attempt.attempt_id,
            {"proposal_id": attempt.proposal_id, "outcome": attempt.outcome.value},
        )

    def _update_attempt_locked(self, attempt: ActionAttempt) -> None:
        updated = self._conn().execute(
            """UPDATE attempts SET attempt_number = ?, status = ?,
                before_resource_version = ?, after_resource_version = ?, outcome = ?,
                error_class = ?, json = ? WHERE idempotency_key = ?""",
            (
                attempt.attempt_number,
                attempt.outcome.value,
                attempt.before_resource_version,
                attempt.after_resource_version,
                attempt.outcome.value,
                attempt.sanitized_error_class,
                canonical_json(attempt.model_dump(mode="json")),
                attempt.idempotency_key,
            ),
        )
        if updated.rowcount != 1:
            raise StoreError(reasons.DUPLICATE_ATTEMPT_KEY, "attempt row is missing")

    def _stored_attempt(self, row: sqlite3.Row) -> ActionAttempt:
        try:
            return ActionAttempt.model_validate_json(row["json"])
        except ValueError as error:
            raise StoreError(reasons.CORRUPT_ROW, "the stored attempt is not valid") from error

    def get_attempt_by_key(self, idempotency_key: str) -> ActionAttempt | None:
        row = self._conn().execute(
            "SELECT json FROM attempts WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        return ActionAttempt.model_validate_json(row["json"])

    def get_latest_attempt_for_incident(self, incident_id: str) -> ActionAttempt | None:
        row = self._conn().execute(
            """SELECT attempts.json FROM attempts
               JOIN proposals ON proposals.proposal_id = attempts.proposal_id
               WHERE proposals.incident_id = ? ORDER BY attempts.rowid DESC LIMIT 1""",
            (incident_id,),
        ).fetchone()
        return None if row is None else self._stored_attempt(row)

    def attempt_count(self, proposal_id: str) -> int:
        return self._attempt_count(proposal_id)

    # -- audit -----------------------------------------------------------------------

    def audit_events(self) -> tuple[AuditEvent, ...]:
        rows = self._conn().execute(
            """
            SELECT event_id, occurred_at, kind, subject, detail
            FROM audit_events
            ORDER BY event_id
            """
        ).fetchall()
        events: list[AuditEvent] = []
        for row in rows:
            detail = json.loads(row["detail"])
            events.append(
                AuditEvent(
                    event_id=int(row["event_id"]),
                    occurred_at=str(row["occurred_at"]),
                    kind=str(row["kind"]),
                    subject=str(row["subject"]),
                    detail=detail if isinstance(detail, dict) else {},
                )
            )
        return tuple(events)

    # -- internals -------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _audit(
        self, kind: str, subject: str, detail: Mapping[str, JsonValue] | None = None
    ) -> None:
        self._conn().execute(
            """
            INSERT INTO audit_events (occurred_at, kind, subject, detail)
            VALUES (?, ?, ?, ?)
            """,
            (self._now().isoformat(), kind, subject, canonical_json(detail or {})),
        )

    def _proposal_row(self, proposal_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            """
            SELECT proposal_id, incident_id, digest, json, state, version
            FROM proposals
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        )

    def _stored_proposal(self, row: sqlite3.Row) -> StoredProposal:
        try:
            proposal = ActionProposal.model_validate_json(row["json"])
        except ValueError as error:
            raise StoreError(reasons.CORRUPT_ROW, "the stored proposal is not valid") from error
        return StoredProposal(
            proposal=proposal,
            digest=str(row["digest"]),
            state=ProposalState(row["state"]),
            version=int(row["version"]),
        )

    def _transition_locked(
        self, stored: StoredProposal, target: ProposalState
    ) -> StoredProposal:
        if target not in TRANSITIONS[stored.state]:
            raise StoreError(
                reasons.ILLEGAL_TRANSITION,
                f"proposal cannot move from {stored.state.value} to {target.value}",
            )
        new_version = stored.version + 1
        updated = self._conn().execute(
            """
            UPDATE proposals
            SET state = ?, version = ?
            WHERE proposal_id = ? AND version = ?
            """,
            (target.value, new_version, stored.proposal.proposal_id, stored.version),
        )
        if updated.rowcount != 1:
            raise StoreError(
                reasons.STALE_VERSION, "the proposal changed during the transition"
            )
        self._audit(
            "proposal_transitioned",
            stored.proposal.proposal_id,
            {"from": stored.state.value, "to": target.value},
        )
        return StoredProposal(
            proposal=stored.proposal,
            digest=stored.digest,
            state=target,
            version=new_version,
        )

    def _approval_row(self, approval_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        )

    def _approval_row_for_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self._fetch_one(
            "SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)
        )

    def _stored_approval(self, row: sqlite3.Row) -> StoredApproval:
        try:
            payload = json.loads(row["json"])
            if not isinstance(payload, dict):
                raise ValueError("approval payload must be an object")
            payload["consumed"] = bool(row["consumed"])
            payload["revoked"] = bool(row["revoked"])
            approval = Approval.model_validate(payload)
        except ValueError as error:
            raise StoreError(reasons.CORRUPT_ROW, "the stored approval is not valid") from error
        return StoredApproval(
            approval=approval,
            version=int(row["version"]),
            policy_bundle_digest=str(row["policy_bundle_digest"]),
            environment_id=str(row["environment_id"]),
            incident_id=str(row["incident_id"]),
            target_identity=str(row["target_identity"]),
        )

    def _resolve(self, principal_id: str) -> Principal:
        try:
            principal = self._resolve_principal(principal_id)
        except StoreError:
            raise
        except Exception as error:
            raise StoreError(
                reasons.UNKNOWN_PRINCIPAL, f"unknown principal {principal_id!r}"
            ) from error
        if principal.principal_id != principal_id:
            raise StoreError(
                reasons.UNKNOWN_PRINCIPAL,
                f"resolved principal {principal.principal_id!r} does not match the request",
            )
        return principal

    def _fresh_snapshot(self) -> SimSnapshot:
        if self._snapshot is None:
            raise StoreError(
                reasons.STATE_UNAVAILABLE, "no read-only snapshot source is configured"
            )
        try:
            return self._snapshot()
        except Exception as error:
            raise StoreError(
                reasons.STATE_UNAVAILABLE, "the live state could not be read"
            ) from error

    def _attempt_count(self, proposal_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def _fetch_one(self, sql: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn().execute(sql, parameters).fetchone()
        return row


__all__ = ["DEFAULT_ENVIRONMENT_ID", "MAX_REJECT_REASON_LENGTH", "Store"]
