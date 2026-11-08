from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest import TestCase

from pydantic import ValidationError

from app.agent.evidence_view import snapshot_evidence
from app.agent.identity import ENVIRONMENT_ID
from app.agent.ids import IdFactory, SequentialIdFactory
from app.agent.investigator import InvestigationStatus, Investigator
from app.agent.proposal import bind_policy_metadata
from app.agent.provider import ScriptedModelProvider
from app.domain.schemas import (
    ActionProposal,
    ActionType,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    PolicyDecision,
    PolicyDecisionType,
    RollbackDeploymentArgs,
    ScaleDeploymentArgs,
)
from app.evidence.models import EvidenceContext
from app.policy import reasons
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster, SimSnapshot
from app.store import Store, StoredProposal

from .agent_support import diagnosis_output, proposal_output, response

ROOT = Path(__file__).resolve().parents[3]
FIXED_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
PROPOSAL_INCIDENT_ID = "incident-1"
PROPOSAL_RUN_ID = "run-1"
PROPOSAL_CONTEXT = EvidenceContext(incident_id=PROPOSAL_INCIDENT_ID, run_id=PROPOSAL_RUN_ID)


def scenario_cluster(scenario_id: str = "healthy-replica-loss") -> SimCluster:
    cluster = SimCluster()
    cluster.inject_fault(load_scenarios()[scenario_id])
    return cluster


def scenario_snapshot(scenario_id: str = "healthy-replica-loss") -> SimSnapshot:
    return scenario_cluster(scenario_id).snapshot()


def proposal_for(
    scenario_id: str = "healthy-replica-loss",
    *,
    action: str = "auto",
    now: datetime = FIXED_NOW,
    policy_config: PolicyConfig | None = None,
) -> ActionProposal:
    snapshot = scenario_snapshot(scenario_id)
    config = policy_config or PolicyConfig()
    evidence = snapshot_evidence(
        snapshot, context=PROPOSAL_CONTEXT, observed_at=now
    ).record.evidence_id
    if action == "scale":
        action_type, parameters = "scale_deployment", {"replicas": 1}
    elif action == "restart":
        action_type, parameters = "restart_deployment", {}
    elif action == "auto" and scenario_id == "healthy-replica-loss":
        action_type, parameters = "scale_deployment", {"replicas": 1}
    elif action == "auto" and scenario_id == "readiness-regression":
        action_type, parameters = "restart_deployment", {}
    else:
        action_type, parameters = None, {}
    provider = ScriptedModelProvider(
        [
            response(diagnosis_output([evidence])),
            response(proposal_output([evidence], action_type=action_type, parameters=parameters)),
        ]
    )
    result = Investigator(provider, now=lambda: now, ids=SequentialIdFactory()).investigate(
        incident_id=PROPOSAL_INCIDENT_ID,
        run_id=PROPOSAL_RUN_ID,
        snapshot=snapshot,
    )
    if result.status is not InvestigationStatus.PROPOSED or result.proposal is None:
        raise AssertionError(f"expected a proposal, got {result.escalation_reason}")
    return bind_policy_metadata(
        result.proposal,
        policy_version=config.policy_version,
        bundle_digest=policy_bundle_digest(config),
        proposal_ttl_seconds=config.proposal_ttl_seconds,
        attempt_limit=config.max_attempts,
    )


def incident_for(
    proposal: ActionProposal,
    *,
    scenario_id: str = "healthy-replica-loss",
    status: IncidentStatus = IncidentStatus.INVESTIGATING,
    now: datetime = FIXED_NOW,
) -> Incident:
    scenario = load_scenarios()[scenario_id]
    return Incident(
        schema_version="0.1",
        incident_id=proposal.incident_id,
        scenario_id=scenario_id,
        environment_id=ENVIRONMENT_ID,
        status=status,
        normalized_alert=NormalizedAlert(
            schema_version="0.1",
            alert_id=scenario.alert.alert_id,
            source="scenario",
            summary=scenario.alert.summary,
            observed_at=now,
            payload={"scenario_id": scenario_id},
        ),
        opened_at=now,
        updated_at=now,
        current_run_id=proposal.run_id,
    )


def policy_input_for(
    proposal: ActionProposal,
    snapshot: SimSnapshot,
    incident: Incident,
    *,
    scenario_id: str = "healthy-replica-loss",
    now: datetime = FIXED_NOW,
    attempts: int = 0,
    **overrides: object,
) -> PolicyInput:
    values: dict[str, object] = {
        "proposal_id": proposal.proposal_id,
        "action_type": proposal.action_type,
        "arguments": proposal.arguments,
        "target": proposal.arguments.target,
        "scenario_id": scenario_id,
        "current_snapshot": snapshot,
        "now": now,
        "proposal_created_at": proposal.created_at,
        "proposal_expires_at": proposal.expires_at,
        "environment_id": proposal.environment_id,
        "incident_status": incident.status,
        "attempts_so_far": attempts,
    }
    values.update(overrides)
    return PolicyInput.model_validate(values)


def decide(
    proposal: ActionProposal,
    snapshot: SimSnapshot,
    incident: Incident,
    *,
    config: PolicyConfig | None = None,
    **overrides: object,
) -> PolicyDecision:
    return evaluate(
        config or PolicyConfig(),
        policy_input_for(proposal, snapshot, incident, **cast(Any, overrides)),
    )


class MutableClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class Recorded:
    store: Store
    cluster: SimCluster
    incident: Incident
    proposal: ActionProposal
    decision: PolicyDecision
    stored: StoredProposal

    @property
    def expected_version(self) -> int:
        return self.stored.version


def record(
    db_path: str = ":memory:",
    *,
    scenario_id: str = "healthy-replica-loss",
    action: str = "auto",
    policy_config: PolicyConfig | None = None,
    clock: MutableClock | None = None,
    ids: IdFactory | None = None,
) -> Recorded:
    config = policy_config or PolicyConfig()
    effective_clock = clock or MutableClock()
    proposal = proposal_for(scenario_id, action=action, policy_config=config)
    incident = incident_for(proposal, scenario_id=scenario_id)
    cluster = scenario_cluster(scenario_id)
    store = Store(
        db_path,
        policy_config=config,
        snapshot=cluster.source.snapshot,
        clock=effective_clock,
        ids=ids or SequentialIdFactory(),
    )
    store.save_environment(ENVIRONMENT_ID, cluster.state)
    store.create_incident(incident)
    decision = decide(proposal, cluster.source.snapshot(), incident, config=config)
    stored = store.record_proposal(proposal, decision)
    return Recorded(store, cluster, incident, proposal, decision, stored)


def scale_arguments(proposal: ActionProposal, replicas: int) -> ScaleDeploymentArgs:
    return ScaleDeploymentArgs.model_construct(
        schema_version="0.1",
        target=proposal.arguments.target,
        action_type="scale_deployment",
        replicas=replicas,
    )


class PolicyTableTests(TestCase):
    proposal: ClassVar[ActionProposal] = proposal_for()
    snapshot: ClassVar[SimSnapshot] = scenario_snapshot()
    incident: ClassVar[Incident] = incident_for(proposal)
    config: ClassVar[PolicyConfig] = PolicyConfig()

    @classmethod
    def setUpClass(cls) -> None:
        pass

    def check(self, expected: PolicyDecisionType, code: str, **overrides: object) -> None:
        decision = decide(
            self.proposal, self.snapshot, self.incident, config=self.config, **overrides
        )
        self.assertEqual(decision.decision, expected)
        self.assertEqual(decision.reason_codes, (code,))

    def test_allowed_scale_requires_a_reviewer(self) -> None:
        decision = decide(self.proposal, self.snapshot, self.incident, config=self.config)
        self.assertEqual(decision.decision, PolicyDecisionType.REQUIRE_APPROVAL)
        self.assertEqual(decision.reason_codes, (reasons.APPROVAL_REQUIRED,))
        self.assertEqual(decision.required_approval_roles, ("reviewer",))
        self.assertEqual(decision.policy_version, "policy-lite-1")
        self.assertEqual(decision.constraints.min_replicas, 1)
        self.assertEqual(decision.constraints.max_replicas, 1)

    def test_every_missing_input_denies_with_missing_input(self) -> None:
        for field in PolicyInput.model_fields:
            with self.subTest(field=field):
                self.check(
                    PolicyDecisionType.DENY, reasons.MISSING_INPUT, **{field: None}
                )

    def test_inconsistent_action_and_arguments_deny(self) -> None:
        arguments = self.proposal.arguments
        self.check(
            PolicyDecisionType.DENY,
            reasons.MISSING_INPUT,
            action_type=ActionType.RESTART_DEPLOYMENT,
            arguments=arguments,
        )

    def test_wrong_environment_is_denied(self) -> None:
        self.check(
            PolicyDecisionType.DENY,
            reasons.ENVIRONMENT_NOT_ALLOWED,
            environment_id="kind-other",
        )

    def test_wrong_namespace_is_denied(self) -> None:
        target = self.proposal.arguments.target.model_copy(update={"namespace": "other"})
        self.check(PolicyDecisionType.DENY, reasons.NAMESPACE_NOT_ALLOWED, target=target)

    def test_wrong_deployment_is_denied(self) -> None:
        target = self.proposal.arguments.target.model_copy(update={"name": "other"})
        self.check(PolicyDecisionType.DENY, reasons.TARGET_NOT_ALLOWED, target=target)

    def test_wrong_kind_is_denied(self) -> None:
        target = self.proposal.arguments.target.model_copy(update={"kind": "StatefulSet"})
        self.check(PolicyDecisionType.DENY, reasons.TARGET_NOT_ALLOWED, target=target)

    def test_unknown_scenario_is_denied(self) -> None:
        self.check(
            PolicyDecisionType.DENY,
            reasons.TARGET_NOT_ALLOWED,
            scenario_id="unknown-scenario",
        )

    def test_wrong_uid_is_denied(self) -> None:
        target = self.proposal.arguments.target.model_copy(update={"uid": "other-uid"})
        self.check(PolicyDecisionType.DENY, reasons.TARGET_UID_MISMATCH, target=target)

    def test_missing_uid_is_denied(self) -> None:
        target = self.proposal.arguments.target.model_copy(update={"uid": None})
        self.check(PolicyDecisionType.DENY, reasons.TARGET_UID_MISMATCH, target=target)

    def test_rollback_is_unsupported(self) -> None:
        target = self.proposal.arguments.target
        rollback = RollbackDeploymentArgs(
            schema_version="0.1", target=target, revision="2"
        )
        self.check(
            PolicyDecisionType.DENY,
            reasons.ACTION_NOT_SUPPORTED,
            action_type=ActionType.ROLLBACK_DEPLOYMENT,
            arguments=rollback,
        )

    def test_unknown_action_is_not_allowed(self) -> None:
        argument = SimpleNamespace(action_type="delete_deployment")
        value = PolicyInput.model_construct(
            **{
                **policy_input_for(self.proposal, self.snapshot, self.incident).__dict__,
                "action_type": "delete_deployment",
                "arguments": argument,
            }
        )
        decision = evaluate(self.config, value)
        self.assertEqual(decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(decision.reason_codes, (reasons.ACTION_NOT_ALLOWED,))

    def test_action_outside_the_allow_list_is_denied(self) -> None:
        config = PolicyConfig(action_allowlist=(ActionType.RESTART_DEPLOYMENT,))
        decision = decide(self.proposal, self.snapshot, self.incident, config=config)
        self.assertEqual(decision.reason_codes, (reasons.ACTION_NOT_ALLOWED,))

    def test_scale_replica_bounds(self) -> None:
        cases = {0: False, 1: True, 2: False, -1: False}
        for replicas, allowed in cases.items():
            with self.subTest(replicas=replicas):
                arguments = scale_arguments(self.proposal, replicas)
                decision = decide(
                    self.proposal,
                    self.snapshot,
                    self.incident,
                    arguments=arguments,
                    target=arguments.target,
                )
                if allowed:
                    self.assertEqual(decision.decision, PolicyDecisionType.REQUIRE_APPROVAL)
                else:
                    self.assertEqual(decision.decision, PolicyDecisionType.DENY)
                    self.assertEqual(
                        decision.reason_codes, (reasons.REPLICAS_OUT_OF_BOUNDS,)
                    )

    def test_scale_zero_is_allowed_when_the_bundle_permits_it(self) -> None:
        config = PolicyConfig(min_replicas=0, max_replicas=2)
        arguments = scale_arguments(self.proposal, 0)
        decision = decide(
            self.proposal,
            self.snapshot,
            self.incident,
            config=config,
            arguments=arguments,
            target=arguments.target,
        )
        self.assertEqual(decision.decision, PolicyDecisionType.REQUIRE_APPROVAL)

    def test_expired_at_the_boundary_and_allowed_one_tick_before(self) -> None:
        expires_at = self.proposal.expires_at
        self.check(
            PolicyDecisionType.DENY,
            reasons.PROPOSAL_EXPIRED,
            now=expires_at,
        )
        self.check(
            PolicyDecisionType.REQUIRE_APPROVAL,
            reasons.APPROVAL_REQUIRED,
            now=expires_at - timedelta(seconds=1),
        )

    def test_closed_incident_is_denied(self) -> None:
        self.check(
            PolicyDecisionType.DENY,
            reasons.INCIDENT_CLOSED,
            incident_status=IncidentStatus.CLOSED,
        )

    def test_attempts_at_the_limit_are_denied(self) -> None:
        self.check(
            PolicyDecisionType.DENY,
            reasons.ATTEMPT_LIMIT_REACHED,
            attempts=1,
        )
        self.check(
            PolicyDecisionType.REQUIRE_APPROVAL,
            reasons.APPROVAL_REQUIRED,
            attempts=0,
        )

    def test_allow_exists_but_only_with_a_test_only_bundle(self) -> None:
        config = PolicyConfig(require_approval=False)
        decision = decide(self.proposal, self.snapshot, self.incident, config=config)
        self.assertEqual(decision.decision, PolicyDecisionType.ALLOW)
        self.assertEqual(decision.reason_codes, (reasons.ALLOWED,))
        self.assertEqual(decision.required_approval_roles, ())

    def test_the_digest_changes_when_one_rule_changes(self) -> None:
        default = PolicyConfig()
        changed = PolicyConfig(min_replicas=0)
        self.assertNotEqual(policy_bundle_digest(default), policy_bundle_digest(changed))
        self.assertEqual(
            policy_bundle_digest(default),
            policy_bundle_digest(PolicyConfig()),
        )

    def test_model_risk_text_is_not_an_input(self) -> None:
        with self.assertRaises(ValidationError):
            policy_input_for(
                self.proposal,
                self.snapshot,
                self.incident,
                risk="the model says this is very safe",
            )

    def test_the_same_input_yields_the_same_decision(self) -> None:
        first = decide(self.proposal, self.snapshot, self.incident)
        second = decide(self.proposal, self.snapshot, self.incident)
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_missing_proposal_still_gets_a_stable_decision(self) -> None:
        decision = evaluate(
            self.config,
            policy_input_for(self.proposal, self.snapshot, self.incident, proposal_id=None),
        )
        self.assertEqual(decision.reason_codes, (reasons.MISSING_INPUT,))
        self.assertTrue(decision.decision_id)


class PolicyConfigTests(TestCase):
    def test_invalid_bounds_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            PolicyConfig(min_replicas=2, max_replicas=1)

    def test_requiring_approval_needs_a_role(self) -> None:
        with self.assertRaises(ValidationError):
            PolicyConfig(required_approval_roles=())

    def test_empty_scenario_targets_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            PolicyConfig(scenario_targets={})

    def test_nested_scenario_targets_are_immutable(self) -> None:
        config = PolicyConfig()
        with self.assertRaises(TypeError):
            cast(dict[str, tuple[str, ...]], config.scenario_targets)[
                "healthy-replica-loss"
            ] = ("other",)


class PolicyDecisionIdTests(TestCase):
    def test_the_decision_id_is_deterministic_and_bounded(self) -> None:
        proposal = proposal_for()
        snapshot = scenario_snapshot()
        incident = incident_for(proposal)
        first = evaluate(PolicyConfig(), policy_input_for(proposal, snapshot, incident))
        second = evaluate(PolicyConfig(), policy_input_for(proposal, snapshot, incident))
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertLessEqual(len(first.decision_id), 128)
        self.assertNotEqual(
            first.decision_id,
            evaluate(
                PolicyConfig(min_replicas=0),
                policy_input_for(proposal, snapshot, incident),
            ).decision_id,
        )


class PolicyClockIndependenceTests(TestCase):
    def test_now_is_only_read_from_the_input(self) -> None:
        proposal = proposal_for()
        snapshot = scenario_snapshot()
        incident = incident_for(proposal)
        first = evaluate(PolicyConfig(), policy_input_for(proposal, snapshot, incident))
        later = evaluate(
            PolicyConfig(),
            policy_input_for(proposal, snapshot, incident, now=FIXED_NOW + timedelta(seconds=30)),
        )
        self.assertEqual(first.decision, later.decision)


POLICY_DIR = ROOT / "backend" / "app" / "policy"
STORE_DIR = ROOT / "backend" / "app" / "store"
FORBIDDEN_MODULES = frozenset(
    {"os", "socket", "http", "urllib", "requests", "httpx", "subprocess"}
)
FORBIDDEN_APP_PREFIXES = (
    "app.gateway",
    "app.sim.ports",
    "app.evidence.kubernetes",
    "app.evidence.transport",
)
FORBIDDEN_NAMES = frozenset({"SimCluster", "SimWriter", "ClusterWriter"})
FORBIDDEN_TEXT = ("scenarios/expected", "expected_outcome")


def _is_clock_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in {
        "now",
        "utcnow",
        "time",
    }


def _module_problems(module: str, area: str) -> list[str]:
    problems: list[str] = []
    if module.split(".", 1)[0] in FORBIDDEN_MODULES:
        problems.append(f"forbidden module {module}")
    if any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in FORBIDDEN_APP_PREFIXES
    ):
        problems.append(f"forbidden app module {module}")
    if area == "store" and module.startswith("app.agent"):
        allowed = {"app.agent.identity", "app.agent.proposal", "app.agent.ids"}
        if module not in allowed:
            problems.append(f"store imports an unexpected agent module: {module}")
    return problems


def isolation_violations(source: str, *, area: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                problems.extend(_module_problems(alias.name, area))
                if alias.name in FORBIDDEN_NAMES:
                    problems.append(f"forbidden import name {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            problems.extend(_module_problems(module, area))
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    problems.append(f"forbidden import name {alias.name}")
    for marker in FORBIDDEN_TEXT:
        if marker in source:
            problems.append(f"forbidden text {marker}")
    if area == "policy":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_clock_call(node):
                problems.append("policy.evaluate must not read a clock")
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("app.store", "app.agent.proposal", "app.agent.ids")
            ):
                problems.append(f"policy imports forbidden module {node.module}")
    return problems


class PolicyAndStoreIsolationTests(TestCase):
    def test_policy_and_store_never_reach_a_writer_gateway_or_network(self) -> None:
        for directory in (POLICY_DIR, STORE_DIR):
            area = directory.name
            for path in sorted(directory.glob("*.py")):
                with self.subTest(path=path.name, area=area):
                    self.assertEqual(
                        isolation_violations(path.read_text(encoding="utf-8"), area=area),
                        [],
                    )

    def test_the_scanner_catches_network_imports(self) -> None:
        issues = isolation_violations("import socket\n", area="policy")
        self.assertTrue(any("socket" in issue for issue in issues))

    def test_the_scanner_catches_gateway_imports(self) -> None:
        gateway = isolation_violations(
            "from app.gateway.gateway import Gateway\n", area="store"
        )
        self.assertTrue(any("app.gateway" in issue for issue in gateway))

    def test_the_scanner_catches_cluster_writer_imports(self) -> None:
        writer = isolation_violations("from app.sim.cluster import SimCluster\n", area="store")
        self.assertTrue(any("SimCluster" in issue for issue in writer))

    def test_the_scanner_catches_evaluator_labels(self) -> None:
        issues = isolation_violations(
            'LABELS = "scenarios/expected/labels.json"\n', area="policy"
        )
        self.assertTrue(any("scenarios/expected" in issue for issue in issues))

    def test_policy_does_not_read_a_clock(self) -> None:
        source = (POLICY_DIR / "engine.py").read_text(encoding="utf-8")
        self.assertEqual(isolation_violations(source, area="policy"), [])
        self.assertIn("def evaluate", source)
