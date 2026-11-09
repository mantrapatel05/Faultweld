"""Local API composition root for Faultweld Lite.

The API deliberately uses the same policy, store, gateway and verifier objects as the
operator CLI. It does not expose the simulator writer to route handlers. The database path
is a local demo default under the operating-system temporary directory; deployments should
inject a managed path and an authenticated principal resolver at their composition root.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir

from pydantic import JsonValue

from app.agent.evidence_view import snapshot_evidence, snapshot_payload
from app.agent.identity import ENVIRONMENT_ID
from app.agent.ids import IdFactory, uuid_id
from app.agent.investigator import InvestigationStatus, Investigator
from app.agent.proposal import bind_policy_metadata
from app.agent.provider import ScriptedModelProvider, scripted_response
from app.domain.schemas import (
    ActionOutcome,
    ActionProposal,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    PolicyDecisionType,
    VerificationResult,
    VerificationStatus,
)
from app.evidence.models import EvidenceContext
from app.gateway import VerifierConfig, verify
from app.gateway.factory import build_simulator_gateway
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster
from app.store import DEFAULT_ENVIRONMENT_ID, Store, StoreError
from app.store.models import ProposalState, StoredProposal

DEFAULT_API_DB_PATH = Path(gettempdir()) / "faultweld-api.sqlite3"
SCRIPT_CHOICES = frozenset({"auto", "scale", "restart", "none"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScenarioRunOutcome:
    run_id: str
    incident: Incident
    investigation_status: str
    escalation_reason: str | None
    proposal: StoredProposal | None
    policy_decision: PolicyDecisionType | None
    policy_reason_codes: tuple[str, ...]
    model_calls: int
    tokens_used: int


@dataclass(frozen=True)
class ExecutionOutcome:
    reason_code: str
    executed: bool
    attempt_id: str | None
    attempt_outcome: ActionOutcome | None
    proposal_state: ProposalState | None
    verification: VerificationResult | None
    verification_status: VerificationStatus | None
    writer_operations: int


class ApiRuntime:
    """Per-process simulator state plus short-lived SQLite connections per request."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_API_DB_PATH,
        *,
        policy_config: PolicyConfig | None = None,
        clock: Callable[[], datetime] = _utc_now,
        ids: IdFactory = uuid_id,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_config = policy_config or PolicyConfig()
        self.clock = clock
        self.ids = ids

        self._memory_anchor: Store | None = None
        if str(self.db_path).startswith("file:"):
            # Keep a connection open so request-scoped Store connections share this
            # in-memory database for the lifetime of the API process.
            self._memory_anchor = Store(self.db_path, policy_config=self.policy_config)
            saved_state = self._memory_anchor.load_environment(DEFAULT_ENVIRONMENT_ID)
        else:
            with Store(self.db_path, policy_config=self.policy_config) as bootstrap:
                saved_state = bootstrap.load_environment(DEFAULT_ENVIRONMENT_ID)
        self.cluster = (
            SimCluster.from_state(saved_state) if saved_state is not None else SimCluster()
        )

    def open_store(self) -> Store:
        """Open a connection on the calling thread; callers must close it."""
        return Store(
            self.db_path,
            policy_config=self.policy_config,
            snapshot=self.cluster.source.snapshot,
            clock=self.clock,
            ids=self.ids,
        )

    def close(self) -> None:
        """Release the anchor connection used by a shared in-memory database."""
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    def is_ready(self) -> bool:
        try:
            with self.open_store() as store:
                store.audit_events()
            self.cluster.source.snapshot()
            return True
        except Exception:
            return False

    def run_scenario(self, scenario_id: str, script: str = "auto") -> ScenarioRunOutcome:
        scenarios = load_scenarios()
        scenario = scenarios.get(scenario_id)
        if scenario is None:
            raise StoreError("scenario_not_found", f"scenario {scenario_id!r} does not exist")
        if script not in SCRIPT_CHOICES:
            raise StoreError(
                "invalid_scenario_script", "script must be auto, scale, restart or none"
            )

        # Lite owns one sample deployment. Start each demonstration from the known baseline.
        self.cluster.reset()
        self.cluster.inject_fault(scenario)
        snapshot = self.cluster.snapshot()
        now = self.clock()
        run_id = self.ids("run")
        incident_id = self.ids("incident")
        incident = self._incident(scenario, incident_id, run_id, now)
        context = EvidenceContext(incident_id=incident_id, run_id=run_id)
        evidence = snapshot_evidence(snapshot, context=context, observed_at=now)
        provider = self._scripted_provider(
            scenario_id, script, evidence.record.evidence_id, snapshot_payload(snapshot)
        )
        investigation = Investigator(provider, now=self.clock, ids=self.ids).investigate(
            incident_id=incident_id,
            run_id=run_id,
            snapshot=snapshot,
        )

        with self.open_store() as store:
            store.save_environment(DEFAULT_ENVIRONMENT_ID, self.cluster.state)
            if investigation.status is InvestigationStatus.ESCALATED:
                escalated = incident.model_copy(
                    update={
                        "status": IncidentStatus.ESCALATED,
                        "updated_at": now,
                        "terminal_reason": investigation.escalation_reason,
                    }
                )
                store.create_incident(escalated)
                return ScenarioRunOutcome(
                    run_id=run_id,
                    incident=escalated,
                    investigation_status=investigation.status.value,
                    escalation_reason=investigation.escalation_reason,
                    proposal=None,
                    policy_decision=None,
                    policy_reason_codes=(),
                    model_calls=investigation.model_calls,
                    tokens_used=investigation.tokens_used,
                )

            proposal = investigation.proposal
            if proposal is None:
                raise RuntimeError("proposed investigation did not return a proposal")
            proposal = self._bind_policy(proposal)
            decision = evaluate(
                self.policy_config,
                PolicyInput(
                    proposal_id=proposal.proposal_id,
                    action_type=proposal.action_type,
                    arguments=proposal.arguments,
                    target=proposal.arguments.target,
                    scenario_id=scenario_id,
                    current_snapshot=snapshot,
                    now=self.clock(),
                    proposal_created_at=proposal.created_at,
                    proposal_expires_at=proposal.expires_at,
                    environment_id=proposal.environment_id,
                    incident_status=incident.status,
                    attempts_so_far=0,
                ),
            )
            store.create_incident(incident)
            stored = store.record_proposal(proposal, decision)
            if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
                updated_incident = store.update_incident_status(
                    incident_id, IncidentStatus.AWAITING_APPROVAL
                )
            elif decision.decision is PolicyDecisionType.DENY:
                updated_incident = store.update_incident_status(
                    incident_id,
                    IncidentStatus.ESCALATED,
                    terminal_reason=decision.reason_codes[0],
                )
            else:
                updated_incident = incident
            return ScenarioRunOutcome(
                run_id=run_id,
                incident=updated_incident,
                investigation_status=investigation.status.value,
                escalation_reason=None,
                proposal=stored,
                policy_decision=decision.decision,
                policy_reason_codes=decision.reason_codes,
                model_calls=investigation.model_calls,
                tokens_used=investigation.tokens_used,
            )

    def execute_proposal(self, proposal_id: str) -> ExecutionOutcome:
        with self.open_store() as store:
            stored = store.get_proposal(proposal_id)
            if stored is None:
                raise StoreError("proposal_not_found", f"proposal {proposal_id!r} does not exist")

            gateway, writer = build_simulator_gateway(
                store,
                self.cluster,
                self.policy_config,
                clock=self.clock,
                ids=self.ids,
            )
            result = gateway.execute(proposal_id, stored.proposal.principal_id)
            verification: VerificationResult | None = None
            verification_status: VerificationStatus | None = None
            current = store.get_proposal(proposal_id)

            # Verification is deliberately independent of the writer's return value.
            # Only a persisted EXECUTED proposal is eligible for this step.
            if (
                result.attempt is not None
                and result.attempt.outcome is ActionOutcome.EXECUTED
                and current is not None
                and current.state is ProposalState.EXECUTED
            ):
                incident = store.get_incident(current.proposal.incident_id)
                scenario = (
                    load_scenarios().get(incident.scenario_id) if incident is not None else None
                )
                if incident is None or scenario is None:
                    raise StoreError(
                        "incident_not_found", "execution incident or scenario is missing"
                    )
                current = store.transition(
                    proposal_id, ProposalState.VERIFYING, current.version
                )
                verification = verify(
                    scenario,
                    self.cluster.source,
                    self.cluster.tick,
                    VerifierConfig(),
                    incident_id=incident.incident_id,
                    attempt_id=result.attempt.attempt_id,
                    clock=self.clock,
                    ids=self.ids,
                )
                verification_status = verification.status
                final_state = {
                    VerificationStatus.RECOVERED: ProposalState.RECOVERED,
                    VerificationStatus.NOT_RECOVERED: ProposalState.NOT_RECOVERED,
                    VerificationStatus.UNKNOWN: ProposalState.ESCALATED,
                }[verification.status]
                store.transition(proposal_id, final_state, current.version)
                if verification.status is VerificationStatus.RECOVERED:
                    store.update_incident_status(incident.incident_id, IncidentStatus.CLOSED)
                else:
                    store.update_incident_status(
                        incident.incident_id,
                        IncidentStatus.ESCALATED,
                        terminal_reason=verification.reason_code or verification.status.value,
                    )
                current = store.get_proposal(proposal_id)

            if verification_status is None and current is not None:
                if current.state is ProposalState.RECOVERED:
                    verification_status = VerificationStatus.RECOVERED
                elif current.state is ProposalState.NOT_RECOVERED:
                    verification_status = VerificationStatus.NOT_RECOVERED
                elif (
                    current.state is ProposalState.ESCALATED
                    and result.attempt is not None
                    and result.attempt.outcome is ActionOutcome.EXECUTED
                ):
                    verification_status = VerificationStatus.UNKNOWN

            store.save_environment(DEFAULT_ENVIRONMENT_ID, self.cluster.state)
            return ExecutionOutcome(
                reason_code=result.reason_code,
                executed=result.executed,
                attempt_id=result.attempt.attempt_id if result.attempt is not None else None,
                attempt_outcome=(
                    result.attempt.outcome if result.attempt is not None else None
                ),
                proposal_state=current.state if current is not None else None,
                verification=verification,
                verification_status=verification_status,
                writer_operations=len(writer.operation_log),
            )

    def mutate_demo_state(
        self,
        incident_id: str,
        *,
        desired_replicas: int | None,
        readiness_path: str | None,
        readiness_port: int | str | None,
    ) -> None:
        with self.open_store() as store:
            if store.get_incident(incident_id) is None:
                raise StoreError("incident_not_found", f"incident {incident_id!r} does not exist")
            self.cluster.mutate_state(
                desired_replicas=desired_replicas,
                readiness_path=readiness_path,
                readiness_port=readiness_port,
            )
            store.save_environment(DEFAULT_ENVIRONMENT_ID, self.cluster.state)

    def _bind_policy(self, proposal: ActionProposal) -> ActionProposal:
        return bind_policy_metadata(
            proposal,
            policy_version=self.policy_config.policy_version,
            bundle_digest=policy_bundle_digest(self.policy_config),
            proposal_ttl_seconds=self.policy_config.proposal_ttl_seconds,
            attempt_limit=self.policy_config.max_attempts,
        )

    @staticmethod
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

    @staticmethod
    def _scripted_provider(
        scenario_id: str,
        script: str,
        evidence_id: str,
        payload: dict[str, JsonValue],
    ) -> ScriptedModelProvider:
        if script == "none" or (script == "auto" and scenario_id == "deployment-unavailable"):
            action_type: str | None = None
            parameters: dict[str, JsonValue] = {}
        elif script == "scale" or (script == "auto" and scenario_id == "healthy-replica-loss"):
            action_type = "scale_deployment"
            parameters = {"replicas": 1}
        else:
            action_type = "restart_deployment"
            parameters = {}

        diagnosis: dict[str, JsonValue] = {
            "hypotheses": [
                {
                    "statement": (
                        f"{scenario_id}: desired={payload.get('desired_replicas')} "
                        f"ready={payload.get('ready_replicas')}"
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
            "rationale": f"deterministic API demonstration for {scenario_id}",
            "evidence_ids": [evidence_id],
        }
        return ScriptedModelProvider(
            [
                scripted_response(diagnosis),
                scripted_response(proposal),
            ]
        )


__all__ = ["ApiRuntime", "ExecutionOutcome", "ScenarioRunOutcome"]
