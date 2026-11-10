"""Live-model evaluation runner: configuration, cost guard, and the per-run pipeline.

This module records observations only. It never reads the evaluator's expectations; the
graders do. The model provider is injected, so tests use scripted or replayed providers and
CI never needs a key or a network. Writes happen only inside the gateway.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.agent.identity import ENVIRONMENT_ID
from app.agent.ids import SequentialIdFactory
from app.agent.investigator import InvestigationBudget, InvestigationStatus, Investigator
from app.agent.llm.pricing import token_cost_usd
from app.agent.proposal import bind_policy_metadata
from app.agent.provider import (
    ModelProvider,
    ModelProviderError,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
)
from app.domain.schemas import (
    ActionOutcome,
    ActionProposal,
    Incident,
    IncidentStatus,
    NormalizedAlert,
    PolicyDecisionType,
    ScaleDeploymentArgs,
    VerificationStatus,
)
from app.evals.attacks import Attack, place
from app.evidence.models import EvidenceContext, EvidenceItem
from app.gateway import VerifierConfig, verify
from app.gateway.factory import build_simulator_gateway
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.policy.engine import PolicyInput, evaluate
from app.policy.principals import LOCAL_DEMO_USER_ID
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster
from app.store import DEFAULT_ENVIRONMENT_ID, ProposalState, Store, StoreError

EVAL_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SCENARIOS = ("healthy-replica-loss", "readiness-regression", "deployment-unavailable")
STALE_PROBE_CASE = "stale-probe"
ENDED_COMPLETED = "completed"
ENDED_COST_CAP = "cost_cap"
Mode = Literal["single-shot", "agentic"]


def eval_clock() -> datetime:
    """A fixed domain clock keeps requests, IDs and transcripts reproducible."""
    return EVAL_NOW


@dataclass(frozen=True)
class LiveCase:
    case_id: str
    scenario_id: str
    attack: Attack | None = None
    stale_probe: bool = False


def build_cases(attacks: tuple[Attack, ...]) -> tuple[LiveCase, ...]:
    base = tuple(LiveCase(f"base-{scenario}", scenario) for scenario in SCENARIOS)
    attacked = tuple(
        LiveCase(f"attack-{attack.attack_id}", "healthy-replica-loss", attack=attack)
        for attack in attacks
    )
    probe = LiveCase(STALE_PROBE_CASE, "healthy-replica-loss", stale_probe=True)
    return (*base, *attacked, probe)


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str
    provider_id: str = "openrouter"
    mode: Mode = "agentic"
    repeats: int = Field(default=3, ge=1)
    max_cost_usd: Decimal = Decimal("1.00")
    input_price_per_million_usd: Decimal = Decimal("0")
    output_price_per_million_usd: Decimal = Decimal("0")
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    budgets: InvestigationBudget = InvestigationBudget()


# -- cost control -----------------------------------------------------------------------


def worst_case_run_cost(config: LiveConfig) -> Decimal:
    """Upper bound for one run: the whole token budget billed at the dearer price."""
    return token_cost_usd(
        input_tokens=0,
        output_tokens=config.budgets.max_tokens,
        input_price_per_million_usd=Decimal("0"),
        output_price_per_million_usd=max(
            config.input_price_per_million_usd, config.output_price_per_million_usd
        ),
    )


@dataclass(frozen=True)
class LiveEstimate:
    runs: int
    max_model_calls: int
    worst_case_cost_usd: Decimal


def estimate_live(cases: tuple[LiveCase, ...], config: LiveConfig) -> LiveEstimate:
    runs = len(cases) * config.repeats
    return LiveEstimate(
        runs=runs,
        max_model_calls=runs * config.budgets.max_model_calls,
        worst_case_cost_usd=worst_case_run_cost(config) * runs,
    )


@dataclass
class CostGuard:
    """A hard cap across the whole eval run. It never lets a run start that could exceed it."""

    cap_usd: Decimal
    worst_case_run_usd: Decimal
    spent_usd: Decimal = Decimal("0")

    def can_start_run(self) -> bool:
        if self.cap_usd <= 0:
            return False
        return self.spent_usd + self.worst_case_run_usd <= self.cap_usd

    def record(self, cost_usd: Decimal) -> None:
        self.spent_usd += cost_usd


class MeteredProvider:
    """Counts calls, tokens, cost and latency around any provider, failures included."""

    def __init__(self, inner: ModelProvider, config: LiveConfig) -> None:
        self._inner = inner
        self._config = config
        self.calls = 0
        self.step_calls = 0
        self.tokens = 0
        self.cost_usd = Decimal("0")
        self.latency_ms = 0
        self.error_codes: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if request.purpose is ModelPurpose.INVESTIGATE_STEP:
            self.step_calls += 1
        started = time.monotonic()
        try:
            response = self._inner.complete(request)
        except ModelProviderError as error:
            self.tokens += error.tokens_used
            self.error_codes.append(error.code)
            # A failed call may still have been billed; charge at the dearer price.
            self.cost_usd += token_cost_usd(
                input_tokens=0,
                output_tokens=error.tokens_used,
                input_price_per_million_usd=Decimal("0"),
                output_price_per_million_usd=max(
                    self._config.input_price_per_million_usd,
                    self._config.output_price_per_million_usd,
                ),
            )
            raise
        finally:
            self.latency_ms += max(0, int((time.monotonic() - started) * 1000))
        self.tokens += response.usage.total_tokens
        self.cost_usd += Decimal(str(response.usage.cost_usd))
        return response


# -- raw observations -------------------------------------------------------------------


class LiveRunRaw(BaseModel):
    """What happened in one run. No expectations and no labels live here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    case_id: str
    repeat_index: int = Field(ge=1)
    scenario_id: str
    attack_id: str | None = None
    stale_probe: bool = False
    mode: Mode
    status: str
    escalation_reason: str | None = None
    model_calls: int = 0
    provider_calls: int = 0
    step_calls: int = 0
    provider_error_codes: tuple[str, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    proposal_action: str | None = None
    proposal_replicas: int | None = None
    policy_decision: str | None = None
    policy_reason_codes: tuple[str, ...] = ()
    approved: bool = False
    gateway_reason: str | None = None
    stale_probe_reason: str | None = None
    writer_operations: int = 0
    verification_status: str | None = None
    final_healthy: bool = False
    incident_status: str


class LiveRunMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    git_sha: str
    run_mode: Literal["live", "replay"]
    mode: Mode
    provider_id: str
    model_id: str
    parameters: dict[str, JsonValue]
    prompt_version: str
    llm_schema_version: str
    policy_version: str
    policy_bundle_digest: str
    min_replicas: int
    max_replicas: int
    repeats: int
    case_ids: tuple[str, ...]
    attack_corpus_digest: str
    max_cost_usd: str
    spent_usd: str
    input_price_per_million_usd: str
    output_price_per_million_usd: str
    runs_planned: int
    runs_completed: int
    stop_reason: str
    started_at: datetime
    ended_at: datetime
    transcript_digests: dict[str, str] = Field(default_factory=dict)


def _healthy(cluster: SimCluster) -> bool:
    snapshot = cluster.snapshot()
    observation = snapshot.observation
    return (
        observation.desired_replicas >= 1
        and observation.available_replicas >= 1
        and observation.ready_replicas >= 1
        and snapshot.readiness_path == "/"
        and str(snapshot.readiness_port) == "http"
    )


def execute_run(
    case: LiveCase,
    repeat_index: int,
    provider: ModelProvider,
    config: LiveConfig,
    policy_config: PolicyConfig | None = None,
) -> LiveRunRaw:
    """One investigation through the real policy, store, gateway and verifier."""
    policy = policy_config or PolicyConfig()
    scenario = load_scenarios()[case.scenario_id]
    ids = SequentialIdFactory()
    metered = MeteredProvider(provider, config)
    cluster = SimCluster()
    cluster.inject_fault(scenario)
    snapshot = cluster.snapshot()
    run_id, incident_id = ids("run"), ids("incident")
    context = EvidenceContext(incident_id=incident_id, run_id=run_id)
    base_summary = (
        f"{scenario.alert.summary}. The healthy service target is "
        f"{scenario.health.healthy_replicas} replica(s)."
    )
    extra: tuple[EvidenceItem, ...] = ()
    summary = base_summary
    if case.attack is not None:
        summary, extra = place(
            case.attack, base_alert_summary=base_summary, context=context, observed_at=EVAL_NOW
        )
    investigator = Investigator(
        metered,
        budgets=config.budgets,
        now=eval_clock,
        ids=ids,
        source=cluster.source if config.mode == "agentic" else None,
        agentic=config.mode == "agentic",
    )
    result = investigator.investigate(
        incident_id=incident_id,
        run_id=run_id,
        snapshot=snapshot,
        evidence=extra,
        alert_summary=summary,
    )
    fields: dict[str, object] = {
        "case_id": case.case_id,
        "repeat_index": repeat_index,
        "scenario_id": case.scenario_id,
        "attack_id": case.attack.attack_id if case.attack else None,
        "stale_probe": case.stale_probe,
        "mode": config.mode,
        "status": result.status.value,
        "escalation_reason": result.escalation_reason,
        "model_calls": result.model_calls,
        "provider_calls": metered.calls,
        "step_calls": metered.step_calls,
        "provider_error_codes": tuple(metered.error_codes),
        "tokens": metered.tokens,
        "cost_usd": float(metered.cost_usd),
        "latency_ms": metered.latency_ms,
    }
    with tempfile.TemporaryDirectory() as directory:
        store = Store(
            Path(directory) / "eval.db",
            policy_config=policy,
            snapshot=cluster.source.snapshot,
            clock=eval_clock,
            ids=ids,
        )
        try:
            incident = Incident(
                schema_version="0.1",
                incident_id=incident_id,
                scenario_id=case.scenario_id,
                environment_id=ENVIRONMENT_ID,
                status=IncidentStatus.INVESTIGATING,
                normalized_alert=NormalizedAlert(
                    schema_version="0.1",
                    alert_id=scenario.alert.alert_id,
                    source="scenario",
                    summary=scenario.alert.summary,
                    observed_at=EVAL_NOW,
                    payload={"scenario_id": case.scenario_id},
                ),
                opened_at=EVAL_NOW,
                updated_at=EVAL_NOW,
                current_run_id=run_id,
            )
            store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
            store.create_incident(incident)
            proposal = result.proposal
            if result.status is InvestigationStatus.ESCALATED or proposal is None:
                store.update_incident_status(
                    incident_id,
                    IncidentStatus.ESCALATED,
                    terminal_reason=result.escalation_reason,
                )
                fields["incident_status"] = IncidentStatus.ESCALATED.value
                fields["final_healthy"] = _healthy(cluster)
                return LiveRunRaw.model_validate(fields)
            fields.update(_run_proposal(case, proposal, store, cluster, policy, ids))
        finally:
            store.close()
    return LiveRunRaw.model_validate(fields)


def _run_proposal(
    case: LiveCase,
    proposal: ActionProposal,
    store: Store,
    cluster: SimCluster,
    policy: PolicyConfig,
    ids: SequentialIdFactory,
) -> dict[str, object]:
    proposal = bind_policy_metadata(
        proposal,
        policy_version=policy.policy_version,
        bundle_digest=policy_bundle_digest(policy),
        proposal_ttl_seconds=policy.proposal_ttl_seconds,
        attempt_limit=policy.max_attempts,
    )
    incident_id = proposal.incident_id
    decision = evaluate(
        policy,
        PolicyInput(
            proposal_id=proposal.proposal_id,
            action_type=proposal.action_type,
            arguments=proposal.arguments,
            target=proposal.arguments.target,
            scenario_id=case.scenario_id,
            current_snapshot=cluster.snapshot(),
            now=eval_clock(),
            proposal_created_at=proposal.created_at,
            proposal_expires_at=proposal.expires_at,
            environment_id=proposal.environment_id,
            incident_status=IncidentStatus.INVESTIGATING,
            attempts_so_far=0,
        ),
    )
    store.record_proposal(proposal, decision)
    replicas = (
        proposal.arguments.replicas if isinstance(proposal.arguments, ScaleDeploymentArgs) else None
    )
    out: dict[str, object] = {
        "proposal_action": proposal.action_type.value,
        "proposal_replicas": replicas,
        "policy_decision": decision.decision.value,
        "policy_reason_codes": tuple(decision.reason_codes),
    }
    if decision.decision is PolicyDecisionType.DENY:
        store.update_incident_status(
            incident_id, IncidentStatus.ESCALATED, terminal_reason=decision.reason_codes[0]
        )
        out.update(status="escalated", incident_status="escalated", final_healthy=_healthy(cluster))
        return out
    if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
        store.update_incident_status(incident_id, IncidentStatus.AWAITING_APPROVAL)
        stored = store.get_proposal(proposal.proposal_id)
        assert stored is not None
        # The harness plays the human: it approves every proposal policy lets through.
        store.approve(proposal.proposal_id, stored.version, LOCAL_DEMO_USER_ID)
        out["approved"] = True
    if case.stale_probe:
        cluster.mutate_state(readiness_port=8080)
        store.save_environment(DEFAULT_ENVIRONMENT_ID, cluster.state)
    gateway, writer = build_simulator_gateway(
        store, cluster, policy, clock=eval_clock, ids=ids
    )
    try:
        result = gateway.execute(proposal.proposal_id, proposal.principal_id)
        reason = result.reason_code
    except StoreError as error:
        result = None
        reason = error.code
    out["writer_operations"] = len(writer.operation_log)
    if case.stale_probe:
        out["stale_probe_reason"] = reason
    else:
        out["gateway_reason"] = reason
    verification = None
    if result is not None and result.attempt is not None:
        if result.attempt.outcome is ActionOutcome.EXECUTED:
            current = store.get_proposal(proposal.proposal_id)
            if current is not None and current.state is ProposalState.EXECUTED:
                store.transition(proposal.proposal_id, ProposalState.VERIFYING, current.version)
                verification = verify(
                    load_scenarios()[case.scenario_id],
                    cluster.source,
                    cluster.tick,
                    VerifierConfig(),
                    incident_id=incident_id,
                    attempt_id=result.attempt.attempt_id,
                    clock=eval_clock,
                    ids=ids,
                )
                final = {
                    VerificationStatus.RECOVERED: ProposalState.RECOVERED,
                    VerificationStatus.NOT_RECOVERED: ProposalState.NOT_RECOVERED,
                    VerificationStatus.UNKNOWN: ProposalState.ESCALATED,
                }[verification.status]
                current = store.get_proposal(proposal.proposal_id)
                assert current is not None
                store.transition(proposal.proposal_id, final, current.version)
    if verification is not None:
        out["verification_status"] = verification.status.value
        recovered = verification.status is VerificationStatus.RECOVERED
        status = IncidentStatus.CLOSED if recovered else IncidentStatus.ESCALATED
        store.update_incident_status(
            incident_id, status, terminal_reason=None if recovered else verification.status.value
        )
        out["incident_status"] = status.value
    else:
        store.update_incident_status(
            incident_id, IncidentStatus.ESCALATED, terminal_reason=reason
        )
        out["incident_status"] = IncidentStatus.ESCALATED.value
    out["final_healthy"] = _healthy(cluster)
    return out


# -- the run loop -----------------------------------------------------------------------

ProviderFactory = Callable[[LiveCase, int], ModelProvider]


@dataclass
class LiveOutcome:
    runs: list[LiveRunRaw] = field(default_factory=list)
    stop_reason: str = ENDED_COMPLETED
    spent_usd: Decimal = Decimal("0")
    planned: int = 0
    started_at: datetime = EVAL_NOW
    ended_at: datetime = EVAL_NOW


def run_live(
    cases: tuple[LiveCase, ...],
    config: LiveConfig,
    provider_factory: ProviderFactory,
    *,
    policy_config: PolicyConfig | None = None,
    on_run: Callable[[LiveRunRaw], None] | None = None,
) -> LiveOutcome:
    """Run every case ``repeats`` times, stopping cleanly if the cost cap would be exceeded."""
    guard = CostGuard(config.max_cost_usd, worst_case_run_cost(config))
    outcome = LiveOutcome(planned=len(cases) * config.repeats)
    outcome.started_at = datetime.now(UTC)
    for repeat in range(1, config.repeats + 1):
        for case in cases:
            if not guard.can_start_run():
                outcome.stop_reason = ENDED_COST_CAP
                outcome.spent_usd = guard.spent_usd
                outcome.ended_at = datetime.now(UTC)
                return outcome
            raw = execute_run(
                case, repeat, provider_factory(case, repeat), config, policy_config
            )
            guard.record(Decimal(str(raw.cost_usd)))
            outcome.runs.append(raw)
            if on_run is not None:
                on_run(raw)
    outcome.spent_usd = guard.spent_usd
    outcome.ended_at = datetime.now(UTC)
    return outcome


def corpus_digest(attacks: tuple[Attack, ...]) -> str:
    payload = "\n".join(
        f"{a.attack_id}|{a.goal}|{a.placement}|{a.text}" for a in attacks
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- persistence and reproducibility record ---------------------------------------------

META_FILE = "live-meta.json"
RUNS_DIR = "runs"


def transcript_digest(directory: Path) -> str:
    """SHA-256 over a run's recorded provider calls, in call order. Empty dir -> empty digest."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("call-*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def make_meta(
    *,
    config: LiveConfig,
    outcome: LiveOutcome,
    cases: tuple[LiveCase, ...],
    attacks: tuple[Attack, ...],
    run_mode: Literal["live", "replay"],
    git_sha: str,
    policy_config: PolicyConfig | None = None,
    transcript_digests: dict[str, str] | None = None,
) -> LiveRunMeta:
    from app.agent.llm.prompts import PROMPT_VERSION
    from app.agent.llm.schemas import SCHEMA_VERSION

    policy = policy_config or PolicyConfig()
    return LiveRunMeta(
        git_sha=git_sha,
        run_mode=run_mode,
        mode=config.mode,
        provider_id=config.provider_id,
        model_id=config.model_id,
        parameters=config.parameters,
        prompt_version=PROMPT_VERSION,
        llm_schema_version=SCHEMA_VERSION,
        policy_version=policy.policy_version,
        policy_bundle_digest=policy_bundle_digest(policy),
        min_replicas=policy.min_replicas,
        max_replicas=policy.max_replicas,
        repeats=config.repeats,
        case_ids=tuple(case.case_id for case in cases),
        attack_corpus_digest=corpus_digest(attacks),
        max_cost_usd=str(config.max_cost_usd),
        spent_usd=str(outcome.spent_usd),
        input_price_per_million_usd=str(config.input_price_per_million_usd),
        output_price_per_million_usd=str(config.output_price_per_million_usd),
        runs_planned=outcome.planned,
        runs_completed=len(outcome.runs),
        stop_reason=outcome.stop_reason,
        started_at=outcome.started_at,
        ended_at=outcome.ended_at,
        transcript_digests=transcript_digests or {},
    )


def run_key(raw: LiveRunRaw) -> str:
    return f"{raw.case_id}__r{raw.repeat_index}"


def write_live_raw(directory: Path, meta: LiveRunMeta, runs: list[LiveRunRaw]) -> None:
    (directory / RUNS_DIR).mkdir(parents=True, exist_ok=True)
    for raw in runs:
        (directory / RUNS_DIR / f"{run_key(raw)}.json").write_text(
            raw.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    (directory / META_FILE).write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_live_raw(directory: Path) -> tuple[LiveRunMeta, tuple[LiveRunRaw, ...]]:
    meta = LiveRunMeta.model_validate_json((directory / META_FILE).read_text(encoding="utf-8"))
    paths = sorted((directory / RUNS_DIR).glob("*.json"))
    runs = tuple(
        LiveRunRaw.model_validate_json(path.read_text(encoding="utf-8")) for path in paths
    )
    return meta, runs
