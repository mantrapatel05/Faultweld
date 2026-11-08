from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from pydantic import JsonValue

from app.agent.ids import SequentialIdFactory
from app.domain.schemas import ActionAttempt, ActionOutcome, IncidentStatus
from app.evidence.canonical import canonical_json
from app.gateway import Gateway, GatewayResult
from app.gateway.gateway import (
    EXECUTION_UNKNOWN,
    PRECONDITION_FAILED,
    PRINCIPAL_MISMATCH,
    REPLAYED,
    STATE_CHANGED,
    TARGET_MISMATCH,
)
from app.policy.config import PolicyConfig
from app.policy.principals import LOCAL_DEMO_USER_ID
from app.sim.cluster import EvidenceSource, SimSnapshot
from app.sim.ports import ClusterWriter, WriteOperation, WriteResult
from app.sim.writer import SimWriter
from app.store import Store
from app.store import reasons as store_reasons

from .test_policy import FIXED_NOW, MutableClock, record

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "scripts" / "lite.py"


class GatewayTests(TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.data = record(clock=self.clock)
        self.writer = SimWriter(self.data.cluster)
        self.store = self.data.store
        self.store.approve(
            self.data.proposal.proposal_id,
            self.data.expected_version,
            LOCAL_DEMO_USER_ID,
        )

    def tearDown(self) -> None:
        self.store.close()

    def gateway(
        self,
        *,
        source: EvidenceSource | None = None,
        writer: ClusterWriter | None = None,
        config: PolicyConfig | None = None,
    ) -> Gateway:
        return Gateway(
            self.store,
            source if source is not None else self.data.cluster.source,
            writer if writer is not None else self.writer,
            config or PolicyConfig(),
            clock=self.clock,
            ids=SequentialIdFactory(),
        )

    def execute(
        self,
        *,
        source: EvidenceSource | None = None,
        writer: ClusterWriter | None = None,
        config: PolicyConfig | None = None,
    ) -> GatewayResult:
        return self.gateway(source=source, writer=writer, config=config).execute(
            self.data.proposal.proposal_id, self.data.proposal.principal_id
        )

    def test_happy_path_consumes_approval_and_mutates_once(self) -> None:
        result = self.execute()
        self.assertEqual(result.reason_code, "executed")
        self.assertTrue(result.executed)
        self.assertEqual(len(self.writer.operation_log), 1)
        self.assertEqual(self.data.cluster.state.desired_replicas, 1)
        approval = self.store.get_approval_for_proposal(self.data.proposal.proposal_id)
        assert approval is not None
        self.assertTrue(approval.approval.consumed)
        self.assertEqual(self.store.attempt_count(self.data.proposal.proposal_id), 1)

    def test_sequential_retry_replays_without_a_second_write(self) -> None:
        first = self.execute()
        second = self.execute()
        self.assertEqual(first.reason_code, "executed")
        self.assertEqual(second.reason_code, REPLAYED)
        self.assertEqual(len(self.writer.operation_log), 1)

    def test_same_idempotency_key_with_changed_proposal_digest_conflicts(self) -> None:
        first = self.execute()
        self.assertEqual(first.reason_code, "executed")
        row = self.store._conn().execute(
            "SELECT json FROM proposals WHERE proposal_id = ?",
            (self.data.proposal.proposal_id,),
        ).fetchone()
        payload = json.loads(row["json"])
        payload["policy_version"] = "changed-after-execution"
        self.store._conn().execute(
            "UPDATE proposals SET json = ? WHERE proposal_id = ?",
            (json.dumps(payload), self.data.proposal.proposal_id),
        )
        second = self.execute()
        self.assertEqual(second.reason_code, store_reasons.IDEMPOTENCY_CONFLICT)
        self.assertEqual(len(self.writer.operation_log), 1)

    def test_stale_state_rejection_has_no_attempt_consumption_or_write(self) -> None:
        self.data.cluster.mutate_state(readiness_port=8080)
        before = self.data.cluster.state
        result = self.execute()
        self.assertEqual(result.reason_code, STATE_CHANGED)
        self.assertEqual(self.data.cluster.state, before)
        self.assertEqual(self.writer.operation_log, ())
        self.assertEqual(self.store.attempt_count(self.data.proposal.proposal_id), 0)
        approval = self.store.get_approval_for_proposal(self.data.proposal.proposal_id)
        assert approval is not None
        self.assertFalse(approval.approval.consumed)
        self.assertEqual(self.store.audit_events()[-1].kind, "gateway_rejected")

    def test_change_between_preflight_and_atomic_claim_is_still_side_effect_free(self) -> None:
        claim = self.store.claim_attempt

        def mutate_before_claim(
            approval_id: str,
            attempt: ActionAttempt,
            context: Mapping[str, JsonValue],
        ) -> None:
            self.data.cluster.mutate_state(readiness_port=8080)
            claim(approval_id, attempt, context)

        with patch.object(self.store, "claim_attempt", side_effect=mutate_before_claim):
            result = self.execute()
        self.assertEqual(result.reason_code, STATE_CHANGED)
        self.assertEqual(self.writer.operation_log, ())
        self.assertEqual(self.store.attempt_count(self.data.proposal.proposal_id), 0)
        approval = self.store.get_approval_for_proposal(self.data.proposal.proposal_id)
        assert approval is not None
        self.assertFalse(approval.approval.consumed)

    def test_stale_rejection_covers_replica_probe_path_probe_port_and_tick_heal(self) -> None:
        mutations = (
            {"desired_replicas": 1},
            {"readiness_path": "/changed"},
            {"readiness_port": 8080},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = record(clock=MutableClock())
                try:
                    data.store.approve(
                        data.proposal.proposal_id,
                        data.expected_version,
                        LOCAL_DEMO_USER_ID,
                    )
                    data.cluster.mutate_state(**mutation)
                    writer = SimWriter(data.cluster)
                    result = Gateway(
                        data.store,
                        data.cluster.source,
                        writer,
                        PolicyConfig(),
                        clock=MutableClock(),
                    ).execute(data.proposal.proposal_id, data.proposal.principal_id)
                    self.assertEqual(result.reason_code, STATE_CHANGED)
                    self.assertEqual(writer.operation_log, ())
                    self.assertEqual(data.store.attempt_count(data.proposal.proposal_id), 0)
                finally:
                    data.store.close()

        transient = record(
            scenario_id="deployment-unavailable",
            action="scale",
            clock=MutableClock(),
        )
        try:
            transient.store.approve(
                transient.proposal.proposal_id,
                transient.expected_version,
                LOCAL_DEMO_USER_ID,
            )
            transient.cluster.tick(3)
            writer = SimWriter(transient.cluster)
            result = Gateway(
                transient.store,
                transient.cluster.source,
                writer,
                PolicyConfig(),
                clock=MutableClock(),
            ).execute(transient.proposal.proposal_id, transient.proposal.principal_id)
            self.assertEqual(result.reason_code, STATE_CHANGED)
            self.assertEqual(writer.operation_log, ())
        finally:
            transient.store.close()

    def test_principal_role_revocation_expiry_and_policy_are_rechecked(self) -> None:
        self.assertEqual(
            self.gateway().execute(self.data.proposal.proposal_id, "wrong-caller").reason_code,
            PRINCIPAL_MISMATCH,
        )

        self.data.store.update_incident_status(
            self.data.incident.incident_id, IncidentStatus.CLOSED
        )
        self.assertEqual(self.execute().reason_code, store_reasons.POLICY_DENIED)

    def test_rejected_approval_is_never_executed(self) -> None:
        data = record(clock=MutableClock())
        try:
            data.store.reject(
                data.proposal.proposal_id,
                data.expected_version,
                LOCAL_DEMO_USER_ID,
                reason="declined",
            )
            writer = SimWriter(data.cluster)
            result = Gateway(
                data.store,
                data.cluster.source,
                writer,
                PolicyConfig(),
                clock=MutableClock(),
            ).execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(result.reason_code, store_reasons.APPROVAL_REJECTED)
            self.assertEqual(writer.operation_log, ())
            self.assertEqual(data.store.attempt_count(data.proposal.proposal_id), 0)
        finally:
            data.store.close()

    def test_digest_target_role_and_policy_mismatches_fail_closed(self) -> None:
        conn = self.store._conn()
        original = conn.execute(
            "SELECT json FROM proposals WHERE proposal_id = ?",
            (self.data.proposal.proposal_id,),
        ).fetchone()["json"]
        conn.execute(
            "UPDATE proposals SET digest = ? WHERE proposal_id = ?",
            ("0" * 64, self.data.proposal.proposal_id),
        )
        self.assertEqual(self.execute().reason_code, store_reasons.DIGEST_MISMATCH)
        conn.execute(
            "UPDATE proposals SET digest = ?, json = ? WHERE proposal_id = ?",
            (self.data.stored.digest, original, self.data.proposal.proposal_id),
        )

        approval_row = conn.execute(
            "SELECT approval_id, json FROM approvals WHERE proposal_id = ?",
            (self.data.proposal.proposal_id,),
        ).fetchone()
        payload = json.loads(approval_row["json"])
        payload["approver_role"] = "observer"
        conn.execute(
            "UPDATE approvals SET json = ?, approver_role = ? WHERE approval_id = ?",
            (json.dumps(payload), "observer", approval_row["approval_id"]),
        )
        self.assertEqual(self.execute().reason_code, store_reasons.ROLE_NOT_PERMITTED)

        conn.execute(
            "UPDATE approvals SET json = ?, approver_role = ? WHERE approval_id = ?",
            (
                json.dumps({**payload, "approver_role": "reviewer"}),
                "reviewer",
                approval_row["approval_id"],
            ),
        )
        conn.execute(
            "UPDATE approvals SET target_identity = ? WHERE approval_id = ?",
            ("{}", approval_row["approval_id"]),
        )
        self.assertEqual(self.execute().reason_code, TARGET_MISMATCH)
        conn.execute(
            "UPDATE approvals SET target_identity = ? WHERE approval_id = ?",
            (
                canonical_json(self.data.proposal.arguments.target.model_dump(mode="json")),
                approval_row["approval_id"],
            ),
        )

        self.assertEqual(
            self.gateway(config=PolicyConfig(policy_version="changed")).execute(
                self.data.proposal.proposal_id, self.data.proposal.principal_id
            ).reason_code,
            store_reasons.POLICY_CHANGED,
        )

    def test_expired_proposal_and_approval_are_refused_before_claiming(self) -> None:
        self.clock.now = FIXED_NOW.replace(minute=16)
        result = self.execute()
        self.assertEqual(result.reason_code, store_reasons.APPROVAL_EXPIRED)
        self.assertEqual(self.store.attempt_count(self.data.proposal.proposal_id), 0)

    def test_proposal_expiry_consumption_revocation_and_missing_records(self) -> None:
        row = self.store._conn().execute(
            "SELECT approval_id, json FROM approvals WHERE proposal_id = ?",
            (self.data.proposal.proposal_id,),
        ).fetchone()
        approval_payload = json.loads(row["json"])
        approval_payload["expires_at"] = self.data.proposal.expires_at.replace(
            minute=30
        ).isoformat()
        self.store._conn().execute(
            "UPDATE approvals SET expires_at = ?, json = ? WHERE approval_id = ?",
            (approval_payload["expires_at"], json.dumps(approval_payload), row["approval_id"]),
        )
        self.clock.now = self.data.proposal.expires_at.replace(second=1)
        self.assertEqual(self.execute().reason_code, store_reasons.PROPOSAL_EXPIRED)

        self.clock.now = FIXED_NOW
        self.store._conn().execute(
            "UPDATE approvals SET consumed = 1 WHERE approval_id = ?", (row["approval_id"],)
        )
        self.assertEqual(self.execute().reason_code, store_reasons.APPROVAL_CONSUMED)
        self.store._conn().execute(
            "UPDATE approvals SET consumed = 0 WHERE approval_id = ?", (row["approval_id"],)
        )
        self.store.revoke(row["approval_id"], LOCAL_DEMO_USER_ID)
        self.assertEqual(self.execute().reason_code, "approval_revoked")

        missing_proposal = self.gateway().execute("missing-proposal", "agent-run-run-1")
        self.assertEqual(missing_proposal.reason_code, store_reasons.PROPOSAL_NOT_FOUND)

        data = record(clock=MutableClock())
        try:
            writer = SimWriter(data.cluster)
            result = Gateway(
                data.store,
                data.cluster.source,
                writer,
                PolicyConfig(),
                clock=MutableClock(),
            ).execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(result.reason_code, store_reasons.APPROVAL_NOT_FOUND)
            self.assertEqual(writer.operation_log, ())
        finally:
            data.store.close()

    def test_missing_state_is_audited_and_does_not_claim(self) -> None:
        class BrokenSource:
            def snapshot(self) -> SimSnapshot:
                raise OSError("unavailable")

        result = self.execute(source=BrokenSource())
        self.assertEqual(result.reason_code, store_reasons.STATE_UNAVAILABLE)
        self.assertEqual(self.writer.operation_log, ())
        self.assertEqual(self.store.attempt_count(self.data.proposal.proposal_id), 0)

    def test_precondition_race_is_failed_and_has_no_gateway_write(self) -> None:
        cluster = self.data.cluster

        class RacingWriter(SimWriter):
            def apply(self, operation: WriteOperation) -> WriteResult:
                cluster.mutate_state(readiness_path="/external-change")
                return super().apply(operation)

        writer = RacingWriter(cluster)
        result = self.execute(writer=writer)
        self.assertEqual(result.reason_code, PRECONDITION_FAILED)
        assert result.attempt is not None
        self.assertEqual(result.attempt.outcome, ActionOutcome.FAILED)
        self.assertEqual(writer.operation_log, ())
        approval = self.store.get_approval_for_proposal(self.data.proposal.proposal_id)
        assert approval is not None
        self.assertTrue(approval.approval.consumed)

    def test_lost_response_reconciles_applied_scale_without_duplicate_effect(self) -> None:
        class LossyWriter(SimWriter):
            def apply(self, operation: WriteOperation) -> WriteResult:
                super().apply(operation)
                raise TimeoutError("response lost")

        writer = LossyWriter(self.data.cluster)
        first = self.execute(writer=writer)
        self.assertEqual(first.reason_code, EXECUTION_UNKNOWN)
        assert first.attempt is not None
        self.assertEqual(first.attempt.outcome, ActionOutcome.EXECUTION_UNKNOWN)
        second = self.execute(writer=writer)
        self.assertEqual(second.reason_code, "reconciled")
        assert second.attempt is not None
        self.assertEqual(second.attempt.outcome, ActionOutcome.EXECUTED)
        self.assertEqual(len(writer.operation_log), 1)

    def test_unchanged_unknown_can_retry_once_but_revocation_blocks_retry(self) -> None:
        config = PolicyConfig(max_attempts=2)
        clock = MutableClock()
        data = record(policy_config=config, clock=clock)
        try:
            data.store.approve(
                data.proposal.proposal_id, data.expected_version, LOCAL_DEMO_USER_ID
            )

            class FailBeforeWrite(SimWriter):
                calls = 0

                def apply(self, operation: WriteOperation) -> WriteResult:
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError("no response")
                    return super().apply(operation)

            writer = FailBeforeWrite(data.cluster)
            gateway = Gateway(
                data.store, data.cluster.source, writer, config, clock=clock
            )
            first = gateway.execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(first.reason_code, EXECUTION_UNKNOWN)
            second = gateway.execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(second.reason_code, "executed")
            self.assertEqual(len(writer.operation_log), 1)
            assert second.attempt is not None
            self.assertEqual(second.attempt.attempt_number, 2)
        finally:
            data.store.close()

        revoked = record(policy_config=config, clock=MutableClock())
        try:
            decision = revoked.store.approve(
                revoked.proposal.proposal_id,
                revoked.expected_version,
                LOCAL_DEMO_USER_ID,
            )

            class NoWriteTimeout(SimWriter):
                def apply(self, operation: WriteOperation) -> WriteResult:
                    raise TimeoutError("no response")

            timeout_writer = NoWriteTimeout(revoked.cluster)
            gateway = Gateway(
                revoked.store,
                revoked.cluster.source,
                timeout_writer,
                config,
                clock=MutableClock(),
            )
            gateway.execute(revoked.proposal.proposal_id, revoked.proposal.principal_id)
            revoked.store.revoke(decision.approval.approval.approval_id, LOCAL_DEMO_USER_ID)
            blocked = gateway.execute(revoked.proposal.proposal_id, revoked.proposal.principal_id)
            self.assertEqual(blocked.reason_code, "approval_revoked")
            self.assertEqual(timeout_writer.operation_log, ())
        finally:
            revoked.store.close()

    def test_revocation_racing_retry_claim_is_checked_inside_store_transaction(self) -> None:
        config = PolicyConfig(max_attempts=2)
        data = record(policy_config=config, clock=self.clock)
        data.store.approve(
            data.proposal.proposal_id, data.expected_version, LOCAL_DEMO_USER_ID
        )

        class NoWriteTimeout(SimWriter):
            def apply(self, operation: WriteOperation) -> WriteResult:
                raise TimeoutError("no response")

        try:
            writer = NoWriteTimeout(data.cluster)
            gateway = Gateway(
                data.store, data.cluster.source, writer, config, clock=self.clock
            )
            first = gateway.execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(first.reason_code, EXECUTION_UNKNOWN)
            approval = data.store.get_approval_for_proposal(data.proposal.proposal_id)
            assert approval is not None
            claim_retry = data.store.claim_retry

            def revoke_before_atomic_claim(retry: ActionAttempt) -> ActionAttempt:
                data.store.revoke(approval.approval.approval_id, LOCAL_DEMO_USER_ID)
                return claim_retry(retry)

            with patch.object(
                data.store, "claim_retry", side_effect=revoke_before_atomic_claim
            ):
                result = gateway.execute(data.proposal.proposal_id, data.proposal.principal_id)
            self.assertEqual(result.reason_code, "approval_revoked")
            self.assertEqual(writer.operation_log, ())
        finally:
            data.store.close()

    def test_restart_reconcile_and_ambiguous_effect(self) -> None:
        restart = record(
            scenario_id="readiness-regression",
            action="restart",
            clock=MutableClock(),
        )
        try:
            restart.store.approve(
                restart.proposal.proposal_id,
                restart.expected_version,
                LOCAL_DEMO_USER_ID,
            )

            class RestartLostResponse(SimWriter):
                def apply(self, operation: WriteOperation) -> WriteResult:
                    super().apply(operation)
                    raise TimeoutError("response lost")

            writer = RestartLostResponse(restart.cluster)
            gateway = Gateway(
                restart.store,
                restart.cluster.source,
                writer,
                PolicyConfig(),
                clock=MutableClock(),
            )
            first = gateway.execute(restart.proposal.proposal_id, restart.proposal.principal_id)
            self.assertEqual(first.reason_code, EXECUTION_UNKNOWN)
            second = gateway.execute(restart.proposal.proposal_id, restart.proposal.principal_id)
            self.assertEqual(second.reason_code, "reconciled")
            self.assertEqual(len(writer.operation_log), 1)
        finally:
            restart.store.close()

        ambiguous = record(
            scenario_id="readiness-regression",
            action="restart",
            clock=MutableClock(),
        )
        try:
            ambiguous.store.approve(
                ambiguous.proposal.proposal_id,
                ambiguous.expected_version,
                LOCAL_DEMO_USER_ID,
            )

            class ExternalChangeThenTimeout(SimWriter):
                def apply(self, operation: WriteOperation) -> WriteResult:
                    ambiguous.cluster.mutate_state(readiness_path="/external-change")
                    raise TimeoutError("response lost")

            ambiguous_writer = ExternalChangeThenTimeout(ambiguous.cluster)
            gateway = Gateway(
                ambiguous.store,
                ambiguous.cluster.source,
                ambiguous_writer,
                PolicyConfig(),
                clock=MutableClock(),
            )
            gateway.execute(ambiguous.proposal.proposal_id, ambiguous.proposal.principal_id)
            second = gateway.execute(
                ambiguous.proposal.proposal_id, ambiguous.proposal.principal_id
            )
            self.assertEqual(second.reason_code, EXECUTION_UNKNOWN)
            self.assertEqual(ambiguous_writer.operation_log, ())
        finally:
            ambiguous.store.close()

    def test_concurrent_execution_has_one_side_effect(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "gateway.sqlite3"
            self.store.close()
            data = record(str(path), clock=self.clock)
            data.store.approve(
                data.proposal.proposal_id,
                data.expected_version,
                LOCAL_DEMO_USER_ID,
            )
            data.store.close()
            barrier = Barrier(2)
            writer = SimWriter(data.cluster)

            def execute_one() -> str:
                store = Store(
                    path,
                    policy_config=PolicyConfig(),
                    snapshot=data.cluster.source.snapshot,
                    clock=self.clock,
                )
                try:
                    barrier.wait(timeout=10)
                    return Gateway(
                        store,
                        data.cluster.source,
                        writer,
                        PolicyConfig(),
                        clock=self.clock,
                    ).execute(data.proposal.proposal_id, data.proposal.principal_id).reason_code
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _index: execute_one(), range(2)))
            self.assertEqual(len(writer.operation_log), 1)
            self.assertIn("executed", outcomes)
            self.assertTrue(
                set(outcomes)
                <= {"executed", "replayed", "execution_unknown", "approval_consumed"}
            )
            check_store = Store(path, snapshot=data.cluster.source.snapshot, clock=self.clock)
            self.assertEqual(check_store.attempt_count(data.proposal.proposal_id), 1)
            check_store.close()

    def test_cli_demos_prove_stale_zero_write_and_verified_happy_path(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            for name in ("stale-approval", "happy-path"):
                with self.subTest(name=name):
                    database = Path(directory) / f"{name}.sqlite3"
                    result = subprocess.run(
                        [sys.executable, str(CLI), "demo", name, "--db", str(database)],
                        cwd=ROOT,
                        capture_output=True,
                        check=False,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    if name == "stale-approval":
                        self.assertIn("state_changed", result.stdout)
                        self.assertIn("writer operations: 0", result.stdout)
                        self.assertIn("state unchanged: true", result.stdout)
                    else:
                        self.assertIn("verification: RECOVERED", result.stdout)
                        self.assertIn("writer operations: 1", result.stdout)

    def test_cli_execute_and_verify_survive_process_restarts(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = Path(directory) / "cli-flow.sqlite3"

            def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=30,
                )

            run = invoke("run", "healthy-replica-loss", "--db", str(database))
            self.assertEqual(run.returncode, 0, run.stderr)
            proposal_match = re.search(r"^proposal: (.+)$", run.stdout, re.MULTILINE)
            incident_match = re.search(r"^incident: (.+)$", run.stdout, re.MULTILINE)
            self.assertIsNotNone(proposal_match)
            self.assertIsNotNone(incident_match)
            assert proposal_match is not None and incident_match is not None
            approve = invoke("approve", proposal_match.group(1), "--db", str(database))
            self.assertEqual(approve.returncode, 0, approve.stderr)
            execute = invoke("execute", proposal_match.group(1), "--db", str(database))
            self.assertEqual(execute.returncode, 0, execute.stderr + execute.stdout)
            self.assertIn("reason: executed", execute.stdout)
            replay = invoke("execute", proposal_match.group(1), "--db", str(database))
            self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
            self.assertIn("reason: replayed", replay.stdout)
            self.assertIn("writer operations: 0", replay.stdout)
            verify_result = invoke("verify", incident_match.group(1), "--db", str(database))
            self.assertEqual(
                verify_result.returncode, 0, verify_result.stderr + verify_result.stdout
            )
            self.assertIn("verification: recovered", verify_result.stdout)
