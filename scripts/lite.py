"""Faultweld Lite operator CLI: investigate, approve, execute and verify.

Every command is one process. The SQLite file carries the incident, proposal, approval and
the simulated environment between invocations, which is what makes pause/restart/resume a
real test rather than a same-process shortcut.

There is deliberately no ``--role`` or ``--approver`` flag. The approver is the local demo
identity resolved by the store; a production deployment would replace that resolver with an
identity provider.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import JsonValue  # noqa: E402

from app.agent.evidence_view import snapshot_evidence, snapshot_payload  # noqa: E402
from app.agent.identity import ENVIRONMENT_ID  # noqa: E402
from app.agent.ids import IdFactory, uuid_id  # noqa: E402
from app.agent.investigator import InvestigationStatus, Investigator  # noqa: E402
from app.agent.proposal import bind_policy_metadata  # noqa: E402
from app.agent.provider import ScriptedModelProvider, scripted_response  # noqa: E402
from app.domain.schemas import (  # noqa: E402
    ActionOutcome,
    ActionProposal,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    ScaleDeploymentArgs,
    VerificationStatus,
)
from app.evidence.models import EvidenceContext  # noqa: E402
from app.gateway import Gateway, VerifierConfig, verify  # noqa: E402
from app.policy.config import PolicyConfig, policy_bundle_digest  # noqa: E402
from app.policy.engine import PolicyInput, evaluate  # noqa: E402
from app.policy.principals import LOCAL_DEMO_USER_ID  # noqa: E402
from app.scenarios.models import ScenarioDefinition  # noqa: E402
from app.scenarios.registry import load_scenarios  # noqa: E402
from app.sim.cluster import SimCluster, SimError, SimSnapshot  # noqa: E402
from app.sim.writer import SimWriter  # noqa: E402
from app.store import DEFAULT_ENVIRONMENT_ID, Store, StoreError  # noqa: E402
from app.store.models import ProposalState  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ESCALATED = 3
EXIT_NOT_FOUND = 4
EXIT_REFUSED = 5

DEFAULT_DB_PATH = Path(tempfile.gettempdir()) / "faultweld-lite.db"
SCRIPT_CHOICES = ("auto", "scale", "restart", "none")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _target_line(proposal: ActionProposal) -> str:
    target = proposal.arguments.target
    return f"{target.cluster_id}/{target.namespace}/{target.kind}/{target.name}"


def _action_parameters(
    scenario_id: str, script: str
) -> tuple[str | None, dict[str, JsonValue]]:
    if script == "none":
        return None, {}
    if script == "scale":
        return "scale_deployment", {"replicas": 1}
    if script == "restart":
        return "restart_deployment", {}
    if scenario_id == "healthy-replica-loss":
        return "scale_deployment", {"replicas": 1}
    if scenario_id == "readiness-regression":
        return "restart_deployment", {}
    if scenario_id == "deployment-unavailable":
        return None, {}
    raise SimError(f"no scripted trajectory for scenario {scenario_id!r}")


def _scripted_steps(
    scenario_id: str,
    script: str,
    evidence_id: str,
    snapshot_payload: dict[str, JsonValue],
) -> ScriptedModelProvider:
    action_type, parameters = _action_parameters(scenario_id, script)
    diagnosis: dict[str, JsonValue] = {
        "hypotheses": [
            {
                "statement": (
                    f"{scenario_id}: desired={snapshot_payload['desired_replicas']} "
                    f"ready={snapshot_payload['ready_replicas']}"
                ),
                "confidence": 0.9,
                "supporting_evidence_ids": [evidence_id],
                "contradicting_evidence_ids": [],
            }
        ],
        "missing_evidence": [],
        "confidence": 0.9,
        "is_sufficient": True,
    }
    proposal: dict[str, JsonValue] = {
        "action_type": action_type,
        "parameters": parameters,
        "rationale": f"deterministic demonstration trajectory for {scenario_id}",
        "evidence_ids": [evidence_id],
    }
    return ScriptedModelProvider(
        [scripted_response(diagnosis), scripted_response(proposal)]
    )


def _incident(
    scenario: ScenarioDefinition, incident_id: str, run_id: str, now: datetime
) -> Incident:
    return Incident(
        schema_version="0.1",
        incident_id=incident_id,
        scenario_id=scenario.scenario_id,
        environment_id=ENVIRONMENT_ID,
        status=IncidentStatus.INVESTIGATING,
        normalized_alert=NormalizedAlert(
            schema_version="0.1",
            alert_id=scenario.alert.alert_id,
            source="scenario",
            summary=scenario.alert.summary,
            observed_at=now,
            payload={"scenario_id": scenario.scenario_id},
        ),
        opened_at=now,
        updated_at=now,
        current_run_id=run_id,
    )


def _read_cluster(db_path: str, policy_config: PolicyConfig) -> SimCluster:
    with Store(db_path, policy_config=policy_config) as probe:
        state = probe.load_environment(DEFAULT_ENVIRONMENT_ID)
    return SimCluster.from_state(state) if state is not None else SimCluster()


def _open_store(db_path: str, policy_config: PolicyConfig) -> tuple[Store, SimCluster]:
    cluster = _read_cluster(db_path, policy_config)
    store = Store(
        db_path,
        policy_config=policy_config,
        snapshot=cluster.source.snapshot,
    )
    return store, cluster


def _policy_input(
    proposal: ActionProposal,
    *,
    scenario_id: str,
    snapshot: SimSnapshot,
    incident_status: IncidentStatus,
    now: datetime,
    attempts: int,
) -> PolicyInput:
    return PolicyInput(
        proposal_id=proposal.proposal_id,
        action_type=proposal.action_type,
        arguments=proposal.arguments,
        target=proposal.arguments.target,
        scenario_id=scenario_id,
        current_snapshot=snapshot,
        now=now,
        proposal_created_at=proposal.created_at,
        proposal_expires_at=proposal.expires_at,
        environment_id=proposal.environment_id,
        incident_status=incident_status,
        attempts_so_far=attempts,
    )


def run_scenario(
    scenario_id: str,
    db_path: str,
    *,
    script: str = "auto",
    policy_config: PolicyConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
    ids: IdFactory = uuid_id,
) -> int:
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        print(f"error: unknown_scenario: {scenario_id}")
        return EXIT_USAGE
    if script not in SCRIPT_CHOICES:
        print(f"error: unknown_script: {script}")
        return EXIT_USAGE
    config = policy_config or PolicyConfig()
    scenario = scenarios[scenario_id]
    now = clock()
    run_id = ids("run")
    incident_id = ids("incident")
    incident = _incident(scenario, incident_id, run_id, now)

    cluster = SimCluster()
    try:
        cluster.inject_fault(scenario)
    except SimError as error:
        print(f"error: invalid_scenario: {error}")
        return EXIT_USAGE
    snapshot = cluster.snapshot()
    context = EvidenceContext(incident_id=incident_id, run_id=run_id)
    evidence = snapshot_evidence(snapshot, context=context, observed_at=now)
    provider = _scripted_steps(
        scenario_id, script, evidence.record.evidence_id, snapshot_payload(snapshot)
    )
    investigator = Investigator(
        provider,
        now=clock,
        ids=ids,
    )
    result = investigator.investigate(
        incident_id=incident_id, run_id=run_id, snapshot=snapshot
    )

    with Store(db_path, policy_config=config) as store:
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
        if result.status is InvestigationStatus.ESCALATED:
            escalated = incident.model_copy(
                update={
                    "status": IncidentStatus.ESCALATED,
                    "updated_at": now,
                    "terminal_reason": result.escalation_reason,
                }
            )
            store.create_incident(escalated)
            print(f"scenario: {scenario_id}")
            print(f"incident: {incident_id}")
            print(f"escalated: {result.escalation_reason}")
            return EXIT_ESCALATED

        proposal = result.proposal
        assert proposal is not None
        proposal = bind_policy_metadata(
            proposal,
            policy_version=config.policy_version,
            bundle_digest=policy_bundle_digest(config),
            proposal_ttl_seconds=config.proposal_ttl_seconds,
            attempt_limit=config.max_attempts,
        )
        store.create_incident(incident)
        decision_now = clock()
        decision = evaluate(
            config,
            _policy_input(
                proposal,
                scenario_id=scenario_id,
                snapshot=snapshot,
                incident_status=incident.status,
                now=decision_now,
                attempts=0,
            ),
        )
        stored = store.record_proposal(proposal, decision)
        print(f"scenario: {scenario_id}")
        print(f"incident: {incident_id}")
        print(f"proposal: {proposal.proposal_id}")
        print(f"decision: {decision.decision.value}")
        print(f"reasons: {','.join(decision.reason_codes)}")
        print(f"state: {stored.state.value}")
        print(f"action: {proposal.action_type.value}")
        print(f"target: {_target_line(proposal)}")
        print(f"evidence: {','.join(proposal.source_evidence_ids)}")
        print(f"expires_at: {proposal.expires_at.isoformat()}")
    return EXIT_OK


def show_proposal(
    proposal_id: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
) -> int:
    config = policy_config or PolicyConfig()
    store, cluster = _open_store(db_path, config)
    try:
        stored = store.get_proposal(proposal_id)
        if stored is None:
            print(f"error: proposal_not_found: {proposal_id}")
            return EXIT_NOT_FOUND
        decision = store.get_decision(proposal_id)
        approval = store.get_approval_for_proposal(proposal_id)
        current = cluster.source.snapshot()
        proposal = stored.proposal
        stale = current.state_fingerprint != proposal.expected_state_fingerprint
        print(f"proposal: {proposal.proposal_id}")
        print(f"state: {stored.state.value}")
        if decision is not None:
            print(f"decision: {decision.decision.decision.value}")
            print(f"reasons: {','.join(decision.decision.reason_codes)}")
        print(f"action: {proposal.action_type.value}")
        print(f"target: {_target_line(proposal)}")
        print(
            "current: "
            f"desired={current.observation.desired_replicas} "
            f"ready={current.observation.ready_replicas} "
            f"available={current.observation.available_replicas} "
            f"revision={current.revision} "
            f"resource_version={current.resource_version}"
        )
        arguments = proposal.arguments
        if isinstance(arguments, ScaleDeploymentArgs):
            print(f"proposed: replicas={arguments.replicas}")
        else:
            print(f"proposed: restart revision={current.revision}")
        print(f"evidence: {','.join(proposal.source_evidence_ids)}")
        print(f"expires_at: {proposal.expires_at.isoformat()}")
        print(f"state_changed: {str(stale).lower()}")
        if stale:
            print("WARNING: state changed since proposal; approval will be refused")
        if approval is not None:
            print(
                f"approval: {approval.approval.approval_id} "
                f"{approval.approval.decision.value} "
                f"by={approval.approval.approver_principal_id} "
                f"expires_at={approval.approval.expires_at.isoformat()} "
                f"revoked={str(approval.approval.revoked).lower()}"
            )
        return EXIT_OK
    finally:
        store.close()


def approve_proposal(
    proposal_id: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
) -> int:
    config = policy_config or PolicyConfig()
    store, _cluster = _open_store(db_path, config)
    try:
        stored = store.get_proposal(proposal_id)
        if stored is None:
            print(f"error: proposal_not_found: {proposal_id}")
            return EXIT_NOT_FOUND
        outcome = store.approve(proposal_id, stored.version, LOCAL_DEMO_USER_ID)
        approval = outcome.approval.approval
        print(f"proposal: {proposal_id}")
        print(f"approval: {approval.approval_id}")
        print(f"decision: {approval.decision.value}")
        print(f"approver: {approval.approver_principal_id}")
        print(f"already_decided: {str(outcome.already_decided).lower()}")
        print(f"expires_at: {approval.expires_at.isoformat()}")
        return EXIT_OK
    except StoreError as error:
        print(f"error: {error}")
        return EXIT_REFUSED
    finally:
        store.close()


def reject_proposal(
    proposal_id: str,
    db_path: str,
    *,
    reason: str,
    policy_config: PolicyConfig | None = None,
) -> int:
    config = policy_config or PolicyConfig()
    store, _cluster = _open_store(db_path, config)
    try:
        stored = store.get_proposal(proposal_id)
        if stored is None:
            print(f"error: proposal_not_found: {proposal_id}")
            return EXIT_NOT_FOUND
        outcome = store.reject(
            proposal_id, stored.version, LOCAL_DEMO_USER_ID, reason=reason
        )
        approval = outcome.approval.approval
        print(f"proposal: {proposal_id}")
        print(f"approval: {approval.approval_id}")
        print(f"decision: {approval.decision.value}")
        print(f"reason: {reason}")
        return EXIT_OK
    except StoreError as error:
        print(f"error: {error}")
        return EXIT_REFUSED
    finally:
        store.close()


def revoke_approval(
    approval_id: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
) -> int:
    config = policy_config or PolicyConfig()
    store, _cluster = _open_store(db_path, config)
    try:
        revoked = store.revoke(approval_id, LOCAL_DEMO_USER_ID)
        print(f"approval: {approval_id}")
        print(f"proposal: {revoked.approval.proposal_id}")
        print("revoked: true")
        return EXIT_OK
    except StoreError as error:
        print(f"error: {error}")
        return EXIT_REFUSED
    finally:
        store.close()


def execute_proposal(
    proposal_id: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
    ids: IdFactory = uuid_id,
) -> int:
    config = policy_config or PolicyConfig()
    store, cluster = _open_store(db_path, config)
    try:
        proposal = store.get_proposal(proposal_id)
        if proposal is None:
            print(f"error: proposal_not_found: {proposal_id}")
            return EXIT_NOT_FOUND
        writer = SimWriter(cluster)
        result = Gateway(store, cluster.source, writer, config, clock=clock, ids=ids).execute(
            proposal_id, proposal.proposal.principal_id
        )
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
        print(f"proposal: {proposal_id}")
        print(f"reason: {result.reason_code}")
        if result.attempt is not None:
            print(f"attempt: {result.attempt.attempt_id}")
            print(f"outcome: {result.attempt.outcome.value}")
        print(f"writer operations: {len(writer.operation_log)}")
        return EXIT_OK if result.executed or result.reason_code == "replayed" else EXIT_REFUSED
    except StoreError as error:
        print(f"error: {error}")
        return EXIT_REFUSED
    finally:
        store.close()


def verify_incident(
    incident_id: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
    verifier_config: VerifierConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
    ids: IdFactory = uuid_id,
) -> int:
    config = policy_config or PolicyConfig()
    store, cluster = _open_store(db_path, config)
    try:
        incident = store.get_incident(incident_id)
        if incident is None:
            print(f"error: incident_not_found: {incident_id}")
            return EXIT_NOT_FOUND
        attempt = store.get_latest_attempt_for_incident(incident_id)
        if attempt is None or attempt.outcome is not ActionOutcome.EXECUTED:
            print("error: no_executed_attempt")
            return EXIT_REFUSED
        proposal = store.get_proposal(attempt.proposal_id)
        if proposal is None or proposal.state not in {
            ProposalState.EXECUTED,
            ProposalState.VERIFYING,
        }:
            print("error: proposal_not_ready_for_verification")
            return EXIT_REFUSED
        scenario = load_scenarios().get(incident.scenario_id)
        if scenario is None:
            print(f"error: unknown_scenario: {incident.scenario_id}")
            return EXIT_REFUSED
        result = verify(
            scenario,
            cluster.source,
            cluster.tick,
            verifier_config or VerifierConfig(),
            incident_id=incident_id,
            attempt_id=attempt.attempt_id,
            clock=clock,
            ids=ids,
        )
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
        if proposal.state is ProposalState.EXECUTED:
            store.transition(attempt.proposal_id, ProposalState.VERIFYING, proposal.version)
        final_state = {
            VerificationStatus.RECOVERED: ProposalState.RECOVERED,
            VerificationStatus.NOT_RECOVERED: ProposalState.NOT_RECOVERED,
            VerificationStatus.UNKNOWN: ProposalState.ESCALATED,
        }[result.status]
        current = store.get_proposal(attempt.proposal_id)
        if current is not None:
            store.transition(attempt.proposal_id, final_state, current.version)
        if result.status is VerificationStatus.RECOVERED:
            store.update_incident_status(incident_id, IncidentStatus.CLOSED)
        else:
            store.update_incident_status(
                incident_id,
                IncidentStatus.ESCALATED,
                terminal_reason=result.reason_code or result.status.value,
            )
        print(f"incident: {incident_id}")
        print(f"verification: {result.status.value}")
        print(f"reason: {result.reason_code or 'none'}")
        for check in result.checks:
            print(f"check {check.check_id}: {check.passed}")
        return EXIT_OK if result.status is VerificationStatus.RECOVERED else EXIT_REFUSED
    finally:
        store.close()


def demo(
    name: str,
    db_path: str,
    *,
    policy_config: PolicyConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
    ids: IdFactory = uuid_id,
) -> int:
    if name not in {"stale-approval", "happy-path"}:
        print(f"error: unknown_demo: {name}")
        return EXIT_USAGE
    config = policy_config or PolicyConfig()
    if run_scenario(
        "healthy-replica-loss", db_path, policy_config=config, clock=clock, ids=ids
    ) != EXIT_OK:
        return EXIT_REFUSED
    store, cluster = _open_store(db_path, config)
    writer = SimWriter(cluster)
    try:
        stored = store.get_latest_proposal()
        if stored is None:
            return EXIT_NOT_FOUND
        store.approve(stored.proposal.proposal_id, stored.version, LOCAL_DEMO_USER_ID)
        print("step: approve")
        gateway = Gateway(store, cluster.source, writer, config, clock=clock, ids=ids)
        if name == "stale-approval":
            cluster.mutate_state(readiness_port=8080)
            store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
            before = cluster.state
            result = gateway.execute(
                stored.proposal.proposal_id, stored.proposal.principal_id
            )
            unchanged = before == cluster.state
            print("step: mutate_state")
            print(f"step: execute refusal {result.reason_code}")
            print(f"writer operations: {len(writer.operation_log)}")
            print(f"state unchanged: {str(unchanged).lower()}")
            return (
                EXIT_OK
                if result.reason_code == "state_changed"
                and not writer.operation_log
                and unchanged
                else EXIT_REFUSED
            )

        result = gateway.execute(stored.proposal.proposal_id, stored.proposal.principal_id)
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
        print(f"step: execute {result.reason_code}")
        if not result.executed or result.attempt is None:
            return EXIT_REFUSED
        verification = verify(
            load_scenarios()["healthy-replica-loss"],
            cluster.source,
            cluster.tick,
            VerifierConfig(),
            incident_id=stored.proposal.incident_id,
            attempt_id=result.attempt.attempt_id,
            clock=clock,
            ids=ids,
        )
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
        current = store.get_proposal(stored.proposal.proposal_id)
        if current is not None:
            store.transition(current.proposal.proposal_id, ProposalState.VERIFYING, current.version)
            current = store.get_proposal(stored.proposal.proposal_id)
        if current is not None and verification.status is VerificationStatus.RECOVERED:
            store.transition(current.proposal.proposal_id, ProposalState.RECOVERED, current.version)
            store.update_incident_status(stored.proposal.incident_id, IncidentStatus.CLOSED)
        print(f"verification: {verification.status.name}")
        print(f"writer operations: {len(writer.operation_log)}")
        return EXIT_OK if verification.status is VerificationStatus.RECOVERED else EXIT_REFUSED
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Faultweld Lite demo CLI.")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="inject a fixture, investigate and classify")
    run_parser.add_argument("scenario_id")
    run_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    run_parser.add_argument("--script", choices=SCRIPT_CHOICES, default="auto")

    show_parser = commands.add_parser("show", help="show one proposal and its approval")
    show_parser.add_argument("proposal_id")
    show_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    approve_parser = commands.add_parser("approve", help="approve a waiting proposal")
    approve_parser.add_argument("proposal_id")
    approve_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    reject_parser = commands.add_parser("reject", help="reject a waiting proposal")
    reject_parser.add_argument("proposal_id")
    reject_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    reject_parser.add_argument("--reason", required=True)

    revoke_parser = commands.add_parser("revoke", help="revoke a recorded approval")
    revoke_parser.add_argument("approval_id")
    revoke_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    execute_parser = commands.add_parser("execute", help="execute an approved proposal")
    execute_parser.add_argument("proposal_id")
    execute_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    verify_parser = commands.add_parser("verify", help="verify an executed incident")
    verify_parser.add_argument("incident_id")
    verify_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))

    demo_parser = commands.add_parser("demo", help="run a complete Lite demonstration")
    demo_parser.add_argument("name", choices=("stale-approval", "happy-path"))
    demo_parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return run_scenario(args.scenario_id, args.db, script=args.script)
    if args.command == "show":
        return show_proposal(args.proposal_id, args.db)
    if args.command == "approve":
        return approve_proposal(args.proposal_id, args.db)
    if args.command == "reject":
        return reject_proposal(args.proposal_id, args.db, reason=args.reason)
    if args.command == "revoke":
        return revoke_approval(args.approval_id, args.db)
    if args.command == "execute":
        return execute_proposal(args.proposal_id, args.db)
    if args.command == "verify":
        return verify_incident(args.incident_id, args.db)
    if args.command == "demo":
        return demo(args.name, args.db)
    return EXIT_USAGE


__all__ = [
    "EXIT_ESCALATED",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "approve_proposal",
    "demo",
    "execute_proposal",
    "main",
    "reject_proposal",
    "revoke_approval",
    "run_scenario",
    "show_proposal",
    "verify_incident",
]


if __name__ == "__main__":
    raise SystemExit(main())
