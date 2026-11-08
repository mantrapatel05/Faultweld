from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.agent.proposal import proposal_digest
from app.domain.schemas import (
    ActionAttempt,
    ActionOutcome,
    ActionPreflightSnapshot,
    ApprovalDecision,
    IncidentStatus,
)
from app.policy.config import PolicyConfig
from app.policy.principals import (
    AGENT_PRINCIPAL_PREFIX,
    LOCAL_DEMO_USER_ID,
    LOCAL_OBSERVER_ID,
)
from app.sim.cluster import SimCluster
from app.store import DEFAULT_ENVIRONMENT_ID, ProposalState, Store, StoreError
from app.store import reasons as store_reasons

from .test_policy import FIXED_NOW, MutableClock, record

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "scripts" / "lite.py"


class StoreLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.data = record(clock=self.clock)

    def tearDown(self) -> None:
        self.data.store.close()

    def test_schema_init_is_idempotent_and_audit_is_append_only(self) -> None:
        self.data.store.init_schema()
        conn = self.data.store._conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_events SET kind = 'changed' WHERE event_id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_events WHERE event_id = 1")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO proposals
                   (proposal_id, incident_id, digest, json, state, version)
                   VALUES ('orphan', 'missing-incident', 'digest', '{}', 'proposed', 1)"""
            )

    def test_illegal_lifecycle_transition_is_refused(self) -> None:
        with self.assertRaises(StoreError) as raised:
            self.data.store.transition(
                self.data.proposal.proposal_id,
                ProposalState.EXECUTED,
                self.data.expected_version,
            )
        self.assertEqual(raised.exception.code, store_reasons.ILLEGAL_TRANSITION)

    def test_duplicate_attempt_key_is_rejected(self) -> None:
        decision = self.data.store.get_decision(self.data.proposal.proposal_id)
        assert decision is not None
        now = FIXED_NOW
        attempt = ActionAttempt(
            schema_version="0.1",
            attempt_id="attempt-1",
            proposal_id=self.data.proposal.proposal_id,
            idempotency_key=self.data.proposal.idempotency_key,
            attempt_number=1,
            preflight=ActionPreflightSnapshot(
                schema_version="0.1",
                target=self.data.proposal.arguments.target,
                state_fingerprint=self.data.proposal.expected_state_fingerprint,
                observed_at=now,
                proposal_digest=proposal_digest(self.data.proposal),
            ),
            authorization_decision_id=decision.decision.decision_id,
            operation_started_at=now,
            operation_finished_at=now,
            outcome=ActionOutcome.FAILED,
            sanitized_error_class="PreconditionFailed",
            trace_id="trace-1",
        )
        self.data.store.insert_attempt(attempt)
        with self.assertRaises(StoreError) as raised:
            self.data.store.insert_attempt(attempt.model_copy(update={"attempt_id": "attempt-2"}))
        self.assertEqual(raised.exception.code, store_reasons.DUPLICATE_ATTEMPT_KEY)

    def test_recorded_proposal_lifecycle_and_idempotent_approval(self) -> None:
        self.assertEqual(self.data.stored.state, ProposalState.AWAITING_APPROVAL)
        approved = self.data.store.approve(
            self.data.proposal.proposal_id,
            self.data.expected_version,
            LOCAL_DEMO_USER_ID,
        )
        self.assertFalse(approved.already_decided)
        self.assertEqual(approved.approval.approval.decision, ApprovalDecision.APPROVE)
        stored = self.data.store.get_proposal(self.data.proposal.proposal_id)
        assert stored is not None
        self.assertEqual(stored.state, ProposalState.AUTHORIZED)
        repeated = self.data.store.approve(
            self.data.proposal.proposal_id,
            self.data.expected_version,
            LOCAL_DEMO_USER_ID,
        )
        self.assertTrue(repeated.already_decided)
        with self.assertRaises(StoreError) as raised:
            self.data.store.reject(
                self.data.proposal.proposal_id,
                self.data.expected_version,
                LOCAL_DEMO_USER_ID,
                reason="changed mind",
            )
        self.assertEqual(raised.exception.code, store_reasons.CONFLICTING_DECISION)

    def test_reject_is_saved_with_a_reason_and_denies_later_approval(self) -> None:
        outcome = self.data.store.reject(
            self.data.proposal.proposal_id,
            self.data.expected_version,
            LOCAL_DEMO_USER_ID,
            reason="operator declined",
        )
        self.assertEqual(outcome.approval.approval.decision, ApprovalDecision.REJECT)
        stored = self.data.store.get_proposal(self.data.proposal.proposal_id)
        assert stored is not None
        self.assertEqual(stored.state, ProposalState.DENIED)
        self.assertEqual(self.data.store.audit_events()[-1].detail["reason"], "operator declined")

    def test_unknown_observer_and_agent_cannot_approve(self) -> None:
        for principal_id, expected in (
            (LOCAL_OBSERVER_ID, store_reasons.ROLE_NOT_PERMITTED),
            (f"{AGENT_PRINCIPAL_PREFIX}{self.data.proposal.run_id}", store_reasons.SELF_APPROVAL),
            ("not-a-known-principal", store_reasons.UNKNOWN_PRINCIPAL),
        ):
            with self.subTest(principal=principal_id), self.assertRaises(StoreError) as raised:
                self.data.store.approve(
                    self.data.proposal.proposal_id,
                    self.data.expected_version,
                    principal_id,
                )
            self.assertEqual(raised.exception.code, expected)

    def test_expired_proposal_stale_state_and_stale_version_are_refused(self) -> None:
        with self.assertRaises(StoreError) as raised:
            self.data.store.approve(
                self.data.proposal.proposal_id,
                self.data.expected_version - 1,
                LOCAL_DEMO_USER_ID,
            )
        self.assertEqual(raised.exception.code, store_reasons.STALE_VERSION)

        self.data.cluster.mutate_state(readiness_port=8080)
        with self.assertRaises(StoreError) as raised:
            self.data.store.approve(
                self.data.proposal.proposal_id,
                self.data.expected_version,
                LOCAL_DEMO_USER_ID,
            )
        self.assertEqual(raised.exception.code, store_reasons.STATE_CHANGED_SINCE_PROPOSAL)

        expired = record(clock=self.clock)
        self.clock.now = expired.proposal.expires_at
        with self.assertRaises(StoreError) as raised:
            expired.store.approve(
                expired.proposal.proposal_id,
                expired.expected_version,
                LOCAL_DEMO_USER_ID,
            )
        self.assertEqual(raised.exception.code, store_reasons.PROPOSAL_EXPIRED)
        expired.store.close()

    def test_closed_incident_is_rechecked_by_policy(self) -> None:
        self.data.store.update_incident_status(
            self.data.incident.incident_id, IncidentStatus.CLOSED
        )
        with self.assertRaises(StoreError) as raised:
            self.data.store.approve(
                self.data.proposal.proposal_id,
                self.data.expected_version,
                LOCAL_DEMO_USER_ID,
            )
        self.assertEqual(raised.exception.code, store_reasons.POLICY_DENIED)

    def test_tampered_proposal_and_changed_policy_are_refused(self) -> None:
        conn = self.data.store._conn()
        row = conn.execute(
            "SELECT json FROM proposals WHERE proposal_id = ?",
            (self.data.proposal.proposal_id,),
        ).fetchone()
        payload = json.loads(row["json"])
        payload["source_evidence_ids"] = ["ev-tampered"]
        conn.execute(
            "UPDATE proposals SET json = ? WHERE proposal_id = ?",
            (json.dumps(payload), self.data.proposal.proposal_id),
        )
        with self.assertRaises(StoreError) as raised:
            self.data.store.approve(
                self.data.proposal.proposal_id,
                self.data.expected_version,
                LOCAL_DEMO_USER_ID,
            )
        self.assertEqual(raised.exception.code, store_reasons.DIGEST_MISMATCH)

    def test_revocation_rebuilds_the_immutable_approval(self) -> None:
        approved = self.data.store.approve(
            self.data.proposal.proposal_id,
            self.data.expected_version,
            LOCAL_DEMO_USER_ID,
        )
        approval_id = approved.approval.approval.approval_id
        revoked = self.data.store.revoke(approval_id, LOCAL_DEMO_USER_ID)
        self.assertTrue(revoked.approval.revoked)
        stored = self.data.store.get_approval(approval_id)
        assert stored is not None
        self.assertTrue(stored.approval.revoked)
        row = self.data.store._conn().execute(
            "SELECT json, revoked FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        payload = json.loads(row["json"])
        payload["revoked"] = False
        self.data.store._conn().execute(
            "UPDATE approvals SET json = ? WHERE approval_id = ?",
            (json.dumps(payload), approval_id),
        )
        stored = self.data.store.get_approval(approval_id)
        assert stored is not None
        self.assertTrue(stored.approval.revoked)


class StorePersistenceTests(TestCase):
    def test_approval_proposal_environment_and_audit_survive_reopen(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "lite.sqlite3"
            clock = MutableClock(FIXED_NOW)
            data = record(str(path), clock=clock)
            approval = data.store.approve(
                data.proposal.proposal_id, data.expected_version, LOCAL_DEMO_USER_ID
            )
            approval_id = approval.approval.approval.approval_id
            proposal_id = data.proposal.proposal_id
            expected_state = data.cluster.state
            data.store.close()

            reopened_cluster = type(data.cluster).from_state(expected_state)
            with Store(
                path,
                snapshot=reopened_cluster.source.snapshot,
                clock=clock,
            ) as reopened:
                stored = reopened.get_proposal(proposal_id)
                assert stored is not None
                self.assertEqual(stored.state, ProposalState.AUTHORIZED)
                reopened_approval = reopened.get_approval(approval_id)
                assert reopened_approval is not None
                self.assertEqual(
                    reopened_approval.approval,
                    approval.approval.approval,
                )
                self.assertEqual(reopened.load_environment("sim"), expected_state)
                kinds = [event.kind for event in reopened.audit_events()]
                self.assertEqual(kinds[-1], "approval_recorded")

    def test_policy_change_invalidates_the_existing_proposal(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "lite.sqlite3"
            data = record(str(path), clock=MutableClock(FIXED_NOW))
            proposal_id = data.proposal.proposal_id
            version = data.expected_version
            snapshot = data.cluster.source.snapshot
            data.store.close()
            changed = PolicyConfig(policy_version="policy-lite-2")
            with Store(
                path,
                policy_config=changed,
                snapshot=snapshot,
                clock=MutableClock(FIXED_NOW),
            ) as reopened:
                with self.assertRaises(StoreError) as raised:
                    reopened.approve(proposal_id, version, LOCAL_DEMO_USER_ID)
            self.assertEqual(raised.exception.code, store_reasons.POLICY_CHANGED)


class LiteCliProcessTests(TestCase):
    def test_reject_and_revoke_commands_across_processes(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)

            def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=30,
                )

            reject_db = root / "reject.sqlite3"
            run_reject = invoke("run", "healthy-replica-loss", "--db", str(reject_db))
            self.assertEqual(run_reject.returncode, 0, run_reject.stderr)
            proposal_match = re.search(r"^proposal: (.+)$", run_reject.stdout, re.MULTILINE)
            self.assertIsNotNone(proposal_match)
            assert proposal_match is not None
            rejected = invoke(
                "reject",
                proposal_match.group(1),
                "--db",
                str(reject_db),
                "--reason",
                "operator declined",
            )
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertIn("decision: reject", rejected.stdout)
            self.assertIn("reason: operator declined", rejected.stdout)

            revoke_db = root / "revoke.sqlite3"
            run_approve = invoke("run", "healthy-replica-loss", "--db", str(revoke_db))
            self.assertEqual(run_approve.returncode, 0, run_approve.stderr)
            proposal_match = re.search(r"^proposal: (.+)$", run_approve.stdout, re.MULTILINE)
            self.assertIsNotNone(proposal_match)
            assert proposal_match is not None
            approved = invoke("approve", proposal_match.group(1), "--db", str(revoke_db))
            self.assertEqual(approved.returncode, 0, approved.stderr)
            approval_match = re.search(r"^approval: (.+)$", approved.stdout, re.MULTILINE)
            self.assertIsNotNone(approval_match)
            assert approval_match is not None
            revoked = invoke("revoke", approval_match.group(1), "--db", str(revoke_db))
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertIn("revoked: true", revoked.stdout)

    def test_run_approve_show_and_conflict_across_processes(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = Path(directory) / "lite.sqlite3"
            run = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "run",
                    "healthy-replica-loss",
                    "--db",
                    str(database),
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("decision: require_approval", run.stdout)
            self.assertIn("action: scale_deployment", run.stdout)
            proposal_match = re.search(r"^proposal: (.+)$", run.stdout, re.MULTILINE)
            self.assertIsNotNone(proposal_match)
            assert proposal_match is not None
            proposal_id = proposal_match.group(1)

            show = subprocess.run(
                [sys.executable, str(CLI), "show", proposal_id, "--db", str(database)],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(show.returncode, 0, show.stderr)
            self.assertIn("state_changed: false", show.stdout)

            approve = subprocess.run(
                [sys.executable, str(CLI), "approve", proposal_id, "--db", str(database)],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(approve.returncode, 0, approve.stderr)
            self.assertIn("approver: local-demo-user", approve.stdout)
            self.assertIn("decision: approve", approve.stdout)

            repeated = subprocess.run(
                [sys.executable, str(CLI), "approve", proposal_id, "--db", str(database)],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("already_decided: true", repeated.stdout)

            with Store(database) as store:
                state = store.load_environment(DEFAULT_ENVIRONMENT_ID)
                assert state is not None
                cluster = SimCluster.from_state(state)
                cluster.mutate_state(readiness_port=8080)
                store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)

            stale_show = subprocess.run(
                [sys.executable, str(CLI), "show", proposal_id, "--db", str(database)],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(stale_show.returncode, 0, stale_show.stderr)
            self.assertIn("state_changed: true", stale_show.stdout)
            self.assertIn("approval will be refused", stale_show.stdout)

            reject = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "reject",
                    proposal_id,
                    "--db",
                    str(database),
                    "--reason",
                    "conflicting decision",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(reject.returncode, 5)
            self.assertIn("conflicting_decision", reject.stdout)

            forged_role = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "approve",
                    proposal_id,
                    "--db",
                    str(database),
                    "--role",
                    "reviewer",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
            self.assertEqual(forged_role.returncode, 2)
            self.assertIn("unrecognized arguments", forged_role.stderr)
