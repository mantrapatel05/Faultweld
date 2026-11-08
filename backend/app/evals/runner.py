"""Run deterministic safety fixtures through Faultweld's real Lite components.

This module intentionally does not import or read evaluator expectations. It records raw
observations so the grader can be changed independently without rerunning the system.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

from pydantic import JsonValue

from app.agent.evidence_view import evidence_item, snapshot_evidence
from app.agent.identity import ENVIRONMENT_ID
from app.agent.ids import SequentialIdFactory
from app.agent.investigator import (
    InvestigationBudget,
    InvestigationResult,
    InvestigationStatus,
    Investigator,
)
from app.agent.proposal import bind_policy_metadata, proposal_digest
from app.agent.provider import (
    ModelProviderError,
    ScriptedModelProvider,
    ScriptStep,
    scripted_response,
)
from app.domain.schemas import (
    ActionAttempt,
    ActionOutcome,
    ActionProposal,
    EvidenceFreshness,
    EvidenceSourceType,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    PolicyDecision,
    PolicyDecisionType,
    VerificationStatus,
)
from app.evals.models import (
    AuthorizationObservation,
    CaseRawResult,
    FixtureCaseSetup,
    FixtureRawResult,
    FixtureSetup,
    StateObservation,
    WriterOperationObservation,
)
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext
from app.gateway import Gateway, VerifierConfig, verify
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.policy.principals import (
    LOCAL_DEMO_USER_ID,
    LOCAL_OBSERVER_ID,
)
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster, SimSnapshot
from app.sim.ports import (
    RestartOperation,
    RollbackOperation,
    ScaleOperation,
    WriteOperation,
    WriteResult,
)
from app.sim.writer import SimWriter
from app.store import Store, StoreError
from app.store.models import ProposalState

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXED_NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FIXED_NOW


def load_fixtures(fixtures_dir: Path = FIXTURES_DIR) -> tuple[FixtureSetup, ...]:
    """Load and validate every setup fixture in stable filename order."""
    fixtures: list[FixtureSetup] = []
    seen: set[str] = set()
    for path in sorted(fixtures_dir.glob("*.json")):
        fixture = FixtureSetup.model_validate_json(path.read_text(encoding="utf-8"))
        if fixture.fixture_id in seen:
            raise ValueError(f"duplicate eval fixture id: {fixture.fixture_id}")
        seen.add(fixture.fixture_id)
        fixtures.append(fixture)
    return tuple(fixtures)


def _resolve_evidence_refs(refs: Sequence[str], evidence_ids: Mapping[str, str]) -> list[str]:
    resolved: list[str] = []
    for ref in refs:
        if ref not in evidence_ids:
            raise ValueError(f"fixture references unknown evidence key {ref!r}")
        resolved.append(evidence_ids[ref])
    return resolved


def _provider_script(
    case: FixtureCaseSetup,
    evidence_ids: Mapping[str, str],
) -> list[ScriptStep]:
    if case.provider_error is not None:
        return [ModelProviderError(case.provider_error, "scripted fixture provider failure")]

    diagnosis_evidence = _resolve_evidence_refs(case.diagnosis_evidence_refs, evidence_ids)
    hypothesis: dict[str, JsonValue] = {
        "statement": f"{case.scenario_id}: the live deployment snapshot identifies the fault",
        "confidence": 0.9,
        "supporting_evidence_ids": [str(item) for item in diagnosis_evidence],
        "contradicting_evidence_ids": [],
    }
    diagnosis: dict[str, JsonValue] = {
        "hypotheses": [hypothesis],
        "missing_evidence": [],
        "confidence": 0.9,
        "is_sufficient": True,
    }
    steps: list[ScriptStep] = [
        scripted_response(diagnosis, input_tokens=40, output_tokens=20)
    ]
    for action in case.model_actions:
        proposal: dict[str, JsonValue] = {
            "action_type": action.action_type,
            "parameters": dict(action.parameters),
            "rationale": action.rationale,
            "evidence_ids": [
                str(item) for item in _resolve_evidence_refs(action.evidence_refs, evidence_ids)
            ],
        }
        steps.append(scripted_response(proposal, input_tokens=35, output_tokens=20))
    return steps


def _incident(scenario_id: str, incident_id: str, run_id: str) -> Incident:
    scenario = load_scenarios()[scenario_id]
    now = _clock()
    return Incident(
        schema_version="0.1",
        incident_id=incident_id,
        scenario_id=scenario_id,
        environment_id=ENVIRONMENT_ID,
        status=IncidentStatus.INVESTIGATING,
        normalized_alert=NormalizedAlert(
            schema_version="0.1",
            alert_id=scenario.alert.alert_id,
            source="eval-fixture",
            summary=scenario.alert.summary,
            observed_at=now,
            payload={"scenario_id": scenario_id, "source": "deterministic-eval"},
        ),
        opened_at=now,
        updated_at=now,
        current_run_id=run_id,
    )


def _policy_decision(
    config: PolicyConfig,
    proposal: ActionProposal,
    snapshot: SimSnapshot,
    incident: Incident,
) -> PolicyDecision:
    return evaluate(
        config,
        PolicyInput(
            proposal_id=proposal.proposal_id,
            action_type=proposal.action_type,
            arguments=proposal.arguments,
            target=proposal.arguments.target,
            scenario_id=incident.scenario_id,
            current_snapshot=snapshot,
            now=_clock(),
            proposal_created_at=proposal.created_at,
            proposal_expires_at=proposal.expires_at,
            environment_id=proposal.environment_id,
            incident_status=incident.status,
            attempts_so_far=0,
        ),
    )


def _approver_id(actor: str, proposal_principal_id: str) -> str:
    if actor == "reviewer":
        return LOCAL_DEMO_USER_ID
    if actor == "observer":
        return LOCAL_OBSERVER_ID
    return proposal_principal_id


def _mutate_after_approval(cluster: SimCluster, case: FixtureCaseSetup) -> None:
    if case.mutate_after_approval == "replicas":
        cluster.mutate_state(desired_replicas=1)
    elif case.mutate_after_approval == "probe_path":
        cluster.mutate_state(readiness_path="/external-change")
    elif case.mutate_after_approval == "probe_port":
        cluster.mutate_state(readiness_port=8080)


def _tamper_stored_proposal(store: Store, proposal_id: str) -> None:
    row = store._conn().execute(
        "SELECT json FROM proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("cannot tamper with a missing proposal")
    payload = json.loads(row["json"])
    payload["policy_version"] = "tampered-after-approval"
    store._conn().execute(
        "UPDATE proposals SET json = ? WHERE proposal_id = ?",
        (canonical_json(payload), proposal_id),
    )
    store._conn().commit()


def _state_observation(cluster: SimCluster) -> StateObservation:
    snapshot = cluster.snapshot()
    observation = snapshot.observation
    return StateObservation(
        desired_replicas=observation.desired_replicas,
        ready_replicas=observation.ready_replicas,
        available_replicas=observation.available_replicas,
        pod_present=cluster.state.pod_present,
        readiness_path=snapshot.readiness_path,
        readiness_port=snapshot.readiness_port,
        revision=snapshot.revision,
        resource_version=snapshot.resource_version,
        tick=snapshot.tick,
    )


def _writer_operations(writer: SimWriter) -> tuple[WriterOperationObservation, ...]:
    observations: list[WriterOperationObservation] = []
    for result in writer.operation_log:
        operation = result.operation
        replicas: int | None = None
        revision: str | None = None
        if isinstance(operation, ScaleOperation):
            operation_type = "scale"
            replicas = operation.replicas
        elif isinstance(operation, RestartOperation):
            operation_type = "restart"
        elif isinstance(operation, RollbackOperation):
            operation_type = "rollback"
            revision = operation.revision
        else:  # pragma: no cover - the write port is a closed union
            operation_type = type(operation).__name__
        observations.append(
            WriterOperationObservation(
                operation_type=operation_type,
                expected_resource_version=operation.expected_resource_version,
                before_resource_version=result.before_resource_version,
                after_resource_version=result.after_resource_version,
                replicas=replicas,
                revision=revision,
            )
        )
    return tuple(observations)


class _LossyWriter(SimWriter):
    def apply(self, operation: WriteOperation) -> WriteResult:
        super().apply(operation)
        raise TimeoutError("simulated response loss after apply")


class _TimeoutBeforeWrite(SimWriter):
    def apply(self, operation: WriteOperation) -> WriteResult:
        raise TimeoutError("simulated timeout before mutation")


class _UnavailableSource:
    def snapshot(self) -> SimSnapshot:
        raise OSError("simulated verifier source outage")


def _authorization_observation(
    *,
    store: Store,
    proposal_id: str,
    caller_principal_id: str,
    policy_config: PolicyConfig,
    cluster: SimCluster,
    clock: Callable[[], datetime],
) -> AuthorizationObservation | None:
    stored = store.get_proposal(proposal_id)
    approval = store.get_approval_for_proposal(proposal_id)
    if stored is None or approval is None:
        return None
    proposal_digest_value = proposal_digest(stored.proposal)
    snapshot = cluster.source.snapshot()
    incident = store.get_incident(stored.proposal.incident_id)
    current_policy_requires_approval = False
    if incident is not None:
        current_decision = evaluate(
            policy_config,
            PolicyInput(
                proposal_id=stored.proposal.proposal_id,
                action_type=stored.proposal.action_type,
                arguments=stored.proposal.arguments,
                target=stored.proposal.arguments.target,
                scenario_id=incident.scenario_id,
                current_snapshot=snapshot,
                now=clock(),
                proposal_created_at=stored.proposal.created_at,
                proposal_expires_at=stored.proposal.expires_at,
                environment_id=stored.proposal.environment_id,
                incident_status=incident.status,
                attempts_so_far=store.attempt_count(proposal_id),
            ),
        )
        current_policy_requires_approval = (
            current_decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
        )
    return AuthorizationObservation(
        approval_present=True,
        approval_is_approve=approval.approval.decision.value == "approve",
        approval_not_revoked=not approval.approval.revoked,
        approval_not_consumed=not approval.approval.consumed,
        approval_not_expired=clock() < approval.approval.expires_at,
        proposal_not_expired=clock() < stored.proposal.expires_at,
        approver_role_permitted=(
            approval.approval.approver_role in policy_config.required_approval_roles
        ),
        caller_matches_proposal=caller_principal_id == stored.proposal.principal_id,
        proposal_digest_matches_stored=proposal_digest_value == stored.digest,
        proposal_digest_matches_approval=(
            proposal_digest_value == approval.approval.proposal_digest
        ),
        target_identity_matches_approval=(
            canonical_json(stored.proposal.arguments.target.model_dump(mode="json"))
            == approval.target_identity
        ),
        policy_version_matches=(
            approval.approval.policy_version == policy_config.policy_version
            and stored.proposal.policy_version == policy_config.policy_version
        ),
        policy_bundle_matches=(
            approval.policy_bundle_digest == policy_bundle_digest(policy_config)
            and stored.proposal.policy_bundle_digest == policy_bundle_digest(policy_config)
        ),
        current_policy_requires_approval=current_policy_requires_approval,
        state_fingerprint_matches_approval=(
            snapshot.state_fingerprint == approval.approval.state_fingerprint
        ),
        state_fingerprint_matches_proposal=(
            snapshot.state_fingerprint == stored.proposal.expected_state_fingerprint
        ),
    )


def _case_result_without_proposal(
    *,
    case: FixtureCaseSetup,
    result: InvestigationResult,
    evidence_ids: Mapping[str, str],
    cluster: SimCluster,
    incident_status: IncidentStatus,
) -> CaseRawResult:
    return CaseRawResult(
        case_id=case.case_id,
        scenario_id=case.scenario_id,
        investigation_status=result.status.value,
        escalation_reason=result.escalation_reason,
        model_calls=result.model_calls,
        tokens_used=result.tokens_used,
        cited_evidence_ids=tuple(
            evidence_id
            for hypothesis in (result.diagnosis.hypotheses if result.diagnosis else ())
            for evidence_id in hypothesis.supporting_evidence_ids
        ),
        evidence_ids_by_key=dict(evidence_ids),
        incident_status=incident_status.value,
        final_state=_state_observation(cluster),
    )


def _run_gateway_calls(
    *,
    case: FixtureCaseSetup,
    db_path: str,
    store: Store,
    cluster: SimCluster,
    writer: SimWriter,
    config: PolicyConfig,
    proposal_id: str,
    caller_principal_id: str,
    gateway_clock: Callable[[], datetime],
) -> tuple[list[str], Store, SimWriter]:
    """Run one, sequential duplicate, or concurrent delivery against the real gateway."""
    updates: dict[str, object] = {}
    if case.policy_version_override is not None:
        updates["policy_version"] = case.policy_version_override
    if case.required_approval_roles_override is not None:
        updates["required_approval_roles"] = case.required_approval_roles_override
    gateway_config = config.model_copy(update=updates) if updates else config

    if case.delivery == "concurrent":
        store.close()
        barrier = Barrier(2)
        losing_claim_finished = Event()

        class CoordinatedStore(Store):
            def claim_attempt(
                self,
                approval_id: str,
                attempt: ActionAttempt,
                execution_context: Mapping[str, JsonValue],
            ) -> None:
                # Hold both callers immediately before the atomic claim. The winner's
                # writer is held until the losing claim observes the consumed approval,
                # so the raw race outcome is repeatable rather than scheduler-dependent.
                barrier.wait(timeout=10)
                try:
                    super().claim_attempt(approval_id, attempt, execution_context)
                except StoreError:
                    losing_claim_finished.set()
                    raise

        class CoordinatedWriter(SimWriter):
            def apply(self, operation: WriteOperation) -> WriteResult:
                if not losing_claim_finished.wait(timeout=10):
                    raise TimeoutError("concurrent loser did not finish its claim")
                return super().apply(operation)

        coordinated_writer = CoordinatedWriter(cluster)

        def execute_one() -> str:
            thread_store = CoordinatedStore(
                db_path,
                policy_config=config,
                snapshot=cluster.source.snapshot,
                clock=_clock,
                ids=SequentialIdFactory(),
            )
            try:
                result = Gateway(
                    thread_store,
                    cluster.source,
                    coordinated_writer,
                    gateway_config,
                    clock=gateway_clock,
                    ids=SequentialIdFactory(),
                ).execute(proposal_id, caller_principal_id)
                return result.reason_code
            finally:
                thread_store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            reasons = list(executor.map(lambda _index: execute_one(), range(2)))
        reopened = Store(
            db_path,
            policy_config=config,
            snapshot=cluster.source.snapshot,
            clock=_clock,
            ids=SequentialIdFactory(),
        )
        return sorted(reasons), reopened, coordinated_writer

    gateway = Gateway(
        store,
        cluster.source,
        writer,
        gateway_config,
        clock=gateway_clock,
        ids=SequentialIdFactory(),
    )
    reasons = [gateway.execute(proposal_id, caller_principal_id).reason_code]
    if case.delivery == "sequential":
        reasons.append(gateway.execute(proposal_id, caller_principal_id).reason_code)
    return reasons, store, writer


def run_case(case: FixtureCaseSetup) -> CaseRawResult:
    """Execute one setup case through the production Lite composition interfaces."""
    scenarios = load_scenarios()
    if case.scenario_id not in scenarios:
        raise ValueError(f"unknown scenario in fixture: {case.scenario_id}")
    scenario = scenarios[case.scenario_id]
    cluster = SimCluster()
    cluster.inject_fault(scenario)
    snapshot = cluster.snapshot()
    run_id = f"run-{case.case_id}"
    incident_id = f"incident-{case.case_id}"
    context = EvidenceContext(incident_id=incident_id, run_id=run_id)
    now = _clock()
    live_item = snapshot_evidence(snapshot, context=context, observed_at=now)
    evidence_by_key = {"live": live_item}
    for extra in case.extra_evidence:
        if extra.key in evidence_by_key:
            raise ValueError(f"duplicate evidence key in fixture: {extra.key}")
        evidence_by_key[extra.key] = evidence_item(
            context=context,
            source_resource=f"Event/{scenario.namespace}/{extra.key}",
            query=f"eval.synthetic-evidence {extra.key}",
            payload={"text": extra.text, "trust": extra.trust},
            observed_at=now,
            source_type=EvidenceSourceType.OTHER,
            freshness={
                "fresh": EvidenceFreshness.FRESH,
                "stale": EvidenceFreshness.STALE,
                "missing": EvidenceFreshness.MISSING,
                "unknown": EvidenceFreshness.UNKNOWN,
            }[extra.freshness],
            evidence_id_prefix=f"ev-{extra.key}",
        )
    evidence_ids = {key: item.record.evidence_id for key, item in evidence_by_key.items()}

    provider = ScriptedModelProvider(_provider_script(case, evidence_ids))
    budget = InvestigationBudget(
        max_model_calls=case.max_model_calls,
        max_tokens=case.max_tokens,
        max_wall_clock_seconds=120,
    )
    investigation = Investigator(
        provider,
        budgets=budget,
        now=_clock,
        ids=SequentialIdFactory(),
    ).investigate(
        incident_id=incident_id,
        run_id=run_id,
        snapshot=snapshot,
        evidence=tuple(item for key, item in evidence_by_key.items() if key != "live"),
    )
    incident = _incident(case.scenario_id, incident_id, run_id)

    with tempfile.TemporaryDirectory(
        prefix="faultweld-eval-", ignore_cleanup_errors=True
    ) as directory:
        db_path = str(Path(directory) / "fixture.sqlite3")
        config = PolicyConfig(max_attempts=case.max_attempts)
        store = Store(
            db_path,
            policy_config=config,
            snapshot=cluster.source.snapshot,
            clock=_clock,
            ids=SequentialIdFactory(),
        )
        store.create_incident(incident)

        if investigation.status is InvestigationStatus.ESCALATED:
            if case.advance_after_investigation_ticks:
                cluster.tick(case.advance_after_investigation_ticks)
            store.update_incident_status(
                incident_id,
                IncidentStatus.ESCALATED,
                terminal_reason=investigation.escalation_reason,
            )
            raw = _case_result_without_proposal(
                case=case,
                result=investigation,
                evidence_ids=evidence_ids,
                cluster=cluster,
                incident_status=IncidentStatus.ESCALATED,
            )
            store.close()
            return raw

        proposal = investigation.proposal
        if proposal is None:  # Defensive: the result validator should make this impossible.
            raise RuntimeError("proposed investigation has no proposal")
        proposal = bind_policy_metadata(
            proposal,
            policy_version=config.policy_version,
            bundle_digest=policy_bundle_digest(config),
            proposal_ttl_seconds=config.proposal_ttl_seconds,
            attempt_limit=config.max_attempts,
        )
        decision = _policy_decision(config, proposal, snapshot, incident)
        stored = store.record_proposal(proposal, decision)
        approval_error: str | None = None
        approval_decision: str | None = None
        second_approval_result: str | None = None
        approval = None
        if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
            approver_id = _approver_id(case.approval_actor, proposal.principal_id)
            try:
                approval_outcome = store.approve(
                    proposal.proposal_id, stored.version, approver_id
                )
                approval = approval_outcome.approval
                approval_decision = approval.approval.decision.value
                if case.repeat_approval:
                    repeated = store.approve(
                        proposal.proposal_id, stored.version, approver_id
                    )
                    second_approval_result = (
                        "already_decided" if repeated.already_decided else "new_decision"
                    )
            except StoreError as error:
                approval_error = error.code

        if approval is None:
            current = store.get_proposal(proposal.proposal_id)
            incident_now = store.get_incident(incident_id)
            assert current is not None and incident_now is not None
            raw = CaseRawResult(
                case_id=case.case_id,
                scenario_id=case.scenario_id,
                investigation_status=investigation.status.value,
                model_calls=investigation.model_calls,
                tokens_used=investigation.tokens_used,
                cited_evidence_ids=tuple(
                    evidence_id
                    for hypothesis in (
                        investigation.diagnosis.hypotheses
                        if investigation.diagnosis
                        else ()
                    )
                    for evidence_id in hypothesis.supporting_evidence_ids
                ),
                proposal_evidence_ids=proposal.source_evidence_ids,
                evidence_ids_by_key=evidence_ids,
                proposal_id=proposal.proposal_id,
                proposal_action=proposal.action_type.value,
                policy_version=config.policy_version,
                policy_bundle_digest=policy_bundle_digest(config),
                proposal_replicas=getattr(proposal.arguments, "replicas", None),
                policy_decision=decision.decision.value,
                policy_reason_codes=decision.reason_codes,
                approval_error=approval_error,
                approval_decision=approval_decision,
                proposal_state=current.state.value,
                incident_status=incident_now.status.value,
                final_state=_state_observation(cluster),
            )
            store.close()
            return raw

        proposal_principal_id = proposal.principal_id
        caller_principal_id = (
            proposal_principal_id if case.caller == "proposal" else "wrong-execution-principal"
        )
        _mutate_after_approval(cluster, case)
        if case.tamper_proposal_after_approval:
            _tamper_stored_proposal(store, proposal.proposal_id)
        if case.revoke_approval_after_approval:
            store.revoke(approval.approval.approval_id, LOCAL_DEMO_USER_ID)

        def gateway_clock() -> datetime:
            if case.expire_approval_after_approval:
                return FIXED_NOW + timedelta(minutes=16)
            return _clock()

        gateway_policy_config = (
            config.model_copy(update={
                **({"policy_version": case.policy_version_override}
                   if case.policy_version_override is not None else {}),
                **({"required_approval_roles": case.required_approval_roles_override}
                   if case.required_approval_roles_override is not None else {}),
            })
            if case.policy_version_override is not None
            or case.required_approval_roles_override is not None
            else config
        )
        authorization = _authorization_observation(
            store=store,
            proposal_id=proposal.proposal_id,
            caller_principal_id=caller_principal_id,
            policy_config=gateway_policy_config,
            cluster=cluster,
            clock=gateway_clock,
        )

        state_at_gateway = _state_observation(cluster)
        if case.writer_fault == "apply_then_timeout":
            writer: SimWriter = _LossyWriter(cluster)
        elif case.writer_fault == "timeout_before_write":
            writer = _TimeoutBeforeWrite(cluster)
        else:
            writer = SimWriter(cluster)

        reasons, store, writer = _run_gateway_calls(
            case=case,
            db_path=db_path,
            store=store,
            cluster=cluster,
            writer=writer,
            config=config,
            proposal_id=proposal.proposal_id,
            caller_principal_id=caller_principal_id,
            gateway_clock=gateway_clock,
        )

        verification_status: str | None = None
        verification_reason: str | None = None
        verification_checks: dict[str, bool | None] = {}
        latest_attempt = store.get_latest_attempt_for_incident(incident_id)
        current = store.get_proposal(proposal.proposal_id)
        if (
            latest_attempt is not None
            and latest_attempt.outcome is ActionOutcome.EXECUTED
            and current is not None
            and current.state is ProposalState.EXECUTED
        ):
            verify_source = _UnavailableSource() if case.verifier_unavailable else cluster.source
            verification = verify(
                scenario,
                verify_source,
                cluster.tick,
                VerifierConfig(),
                incident_id=incident_id,
                attempt_id=latest_attempt.attempt_id,
                clock=_clock,
                ids=SequentialIdFactory(),
            )
            current = store.get_proposal(proposal.proposal_id)
            assert current is not None
            store.transition(proposal.proposal_id, ProposalState.VERIFYING, current.version)
            current = store.get_proposal(proposal.proposal_id)
            assert current is not None
            final_state = {
                VerificationStatus.RECOVERED: ProposalState.RECOVERED,
                VerificationStatus.NOT_RECOVERED: ProposalState.NOT_RECOVERED,
                VerificationStatus.UNKNOWN: ProposalState.ESCALATED,
            }[verification.status]
            store.transition(proposal.proposal_id, final_state, current.version)
            if verification.status is VerificationStatus.RECOVERED:
                store.update_incident_status(incident_id, IncidentStatus.CLOSED)
            else:
                store.update_incident_status(
                    incident_id,
                    IncidentStatus.ESCALATED,
                    terminal_reason=verification.reason_code or verification.status.value,
                )
            verification_status = verification.status.value
            verification_reason = verification.reason_code
            verification_checks = {
                check.check_id: check.passed for check in verification.checks
            }

        current = store.get_proposal(proposal.proposal_id)
        incident_now = store.get_incident(incident_id)
        assert current is not None and incident_now is not None
        latest_attempt = store.get_latest_attempt_for_incident(incident_id)
        approval_after = store.get_approval_for_proposal(proposal.proposal_id)
        attempt_count = store.attempt_count(proposal.proposal_id)
        audit_event_kinds = tuple(
            event.kind for event in store.audit_events() if event.subject == proposal.proposal_id
        )
        raw = CaseRawResult(
            case_id=case.case_id,
            scenario_id=case.scenario_id,
            investigation_status=investigation.status.value,
            model_calls=investigation.model_calls,
            tokens_used=investigation.tokens_used,
            cited_evidence_ids=tuple(
                evidence_id
                for hypothesis in (
                    investigation.diagnosis.hypotheses
                    if investigation.diagnosis
                    else ()
                )
                for evidence_id in hypothesis.supporting_evidence_ids
            ),
            proposal_evidence_ids=proposal.source_evidence_ids,
            evidence_ids_by_key=evidence_ids,
            proposal_id=proposal.proposal_id,
            proposal_action=proposal.action_type.value,
            policy_version=config.policy_version,
            policy_bundle_digest=policy_bundle_digest(config),
            gateway_policy_version=gateway_policy_config.policy_version,
            gateway_policy_bundle_digest=policy_bundle_digest(gateway_policy_config),
            proposal_replicas=getattr(proposal.arguments, "replicas", None),
            policy_decision=decision.decision.value,
            policy_reason_codes=decision.reason_codes,
            approval_error=approval_error,
            approval_decision=approval_decision,
            second_approval_result=second_approval_result,
            approval_presented_to_gateway=True,
            authorization=authorization,
            gateway_reason_codes=tuple(reasons),
            state_at_gateway=state_at_gateway,
            approval_consumed_after_gateway=(
                approval_after.approval.consumed if approval_after is not None else None
            ),
            attempt_count=attempt_count,
            audit_event_kinds=audit_event_kinds,
            attempt_outcomes=(latest_attempt.outcome.value,) if latest_attempt else (),
            idempotency_key=proposal.idempotency_key,
            writer_operations=_writer_operations(writer),
            proposal_state=current.state.value,
            incident_status=incident_now.status.value,
            verification_status=verification_status,
            verification_reason=verification_reason,
            verification_checks=verification_checks,
            final_state=_state_observation(cluster),
        )
        store.close()
        return raw


def _record_runner_error(case: FixtureCaseSetup, error: Exception) -> CaseRawResult:
    return CaseRawResult(
        case_id=case.case_id,
        scenario_id=case.scenario_id,
        investigation_status="runner_error",
        runner_error_class=type(error).__name__[:80],
        incident_status="unknown",
        final_state=None,
    )


def run_fixture(
    fixture: FixtureSetup,
    *,
    repeat_index: int,
    git_sha: str,
) -> FixtureRawResult:
    results_list: list[CaseRawResult] = []
    for case in fixture.cases:
        try:
            results_list.append(run_case(case))
        except Exception as error:
            # Preserve the failed case in raw output. The grader will mark it failed;
            # the runner never drops a result simply because the system under test crashed.
            results_list.append(_record_runner_error(case, error))
    results = tuple(results_list)
    return FixtureRawResult(
        fixture_id=fixture.fixture_id,
        fixture_version=fixture.version,
        repeat_index=repeat_index,
        git_sha=git_sha,
        cases=results,
    )


def run_all(
    *,
    repeats: int,
    output_dir: Path,
    git_sha: str | None = None,
    fixtures: Sequence[FixtureSetup] | None = None,
) -> tuple[Path, ...]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_results = tuple(output_dir.glob("*.json"))
    if existing_results:
        raise FileExistsError(
            f"raw output directory {output_dir} is not empty; use a fresh directory "
            "to avoid mixing results from different runs"
        )
    revision = git_sha or os.environ.get("GITHUB_SHA") or "working-tree"
    selected = tuple(fixtures) if fixtures is not None else load_fixtures()
    written: list[Path] = []
    for fixture in selected:
        for repeat_index in range(1, repeats + 1):
            raw = run_fixture(fixture, repeat_index=repeat_index, git_sha=revision)
            path = output_dir / f"{fixture.fixture_id}-repeat-{repeat_index:02d}.json"
            payload = raw.model_dump_json(indent=2) + "\n"
            path.write_text(payload, encoding="utf-8", newline="\n")
            written.append(path)
    return tuple(written)


__all__ = ["FIXED_NOW", "FIXTURES_DIR", "load_fixtures", "run_all", "run_case", "run_fixture"]
