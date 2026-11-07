"""Shared fixtures for the agent tests. This is deliberately not a test module."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from pydantic import JsonValue

from app.agent.evidence_view import evidence_item, snapshot_evidence
from app.agent.ids import SequentialIdFactory
from app.agent.investigator import InvestigationBudget, Investigator
from app.agent.provider import ModelResponse, ScriptedModelProvider, ScriptStep, scripted_response
from app.domain.schemas import EvidenceFreshness, EvidenceSourceType
from app.evidence.models import EvidenceContext, EvidenceItem
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster, SimSnapshot

FIXED_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CONTEXT = EvidenceContext(incident_id="incident-1", run_id="run-1")


def fixed_now() -> datetime:
    return FIXED_NOW


def scenario_snapshot(scenario_id: str) -> SimSnapshot:
    cluster = SimCluster()
    cluster.inject_fault(load_scenarios()[scenario_id])
    return cluster.snapshot()


def snapshot_item(
    snapshot: SimSnapshot | None = None,
    *,
    context: EvidenceContext = CONTEXT,
) -> EvidenceItem:
    return snapshot_evidence(
        snapshot or scenario_snapshot("healthy-replica-loss"),
        context=context,
        observed_at=FIXED_NOW,
    )


def extra_evidence(
    text: str,
    *,
    context: EvidenceContext = CONTEXT,
    evidence_id_prefix: str = "ev-note",
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
    trust: str = "untrusted",
    payload: Mapping[str, JsonValue] | None = None,
) -> EvidenceItem:
    body: dict[str, JsonValue] = {"trust": trust, "text": text}
    if payload is not None:
        body.update(payload)
    return evidence_item(
        context=context,
        source_resource="Event/faultweld-demo/operator-note",
        query="sim.operator-note",
        payload=body,
        observed_at=FIXED_NOW,
        source_type=EvidenceSourceType.OTHER,
        freshness=freshness,
        evidence_id_prefix=evidence_id_prefix,
    )


def diagnosis_output(
    evidence_ids: Sequence[str],
    *,
    sufficient: bool = True,
    confidence: float = 0.9,
    statement: str = "the sample deployment has no ready replicas",
) -> dict[str, JsonValue]:
    hypothesis: dict[str, JsonValue] = {
        "statement": statement,
        "confidence": confidence,
        "supporting_evidence_ids": list(evidence_ids),
        "contradicting_evidence_ids": [],
    }
    output: dict[str, JsonValue] = {
        "hypotheses": [hypothesis],
        "missing_evidence": [],
        "confidence": confidence,
        "is_sufficient": sufficient,
    }
    return output


def proposal_output(
    evidence_ids: Sequence[str],
    *,
    action_type: str | None = "scale_deployment",
    parameters: Mapping[str, JsonValue] | None = None,
    rationale: str = "restore the expected replica count",
) -> dict[str, JsonValue]:
    output: dict[str, JsonValue] = {
        "action_type": action_type,
        "parameters": dict(parameters) if parameters is not None else {"replicas": 1},
        "rationale": rationale,
        "evidence_ids": list(evidence_ids),
    }
    return output


def response(
    output: dict[str, JsonValue],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ModelResponse:
    return scripted_response(output, input_tokens=input_tokens, output_tokens=output_tokens)


class SequenceClock:
    """Returns the given times in order, then repeats the last one."""

    def __init__(self, values: Sequence[datetime]) -> None:
        if not values:
            raise ValueError("a clock needs at least one value")
        self._values = list(values)
        self._last = values[-1]

    def __call__(self) -> datetime:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def make_investigator(
    script: Sequence[ScriptStep],
    *,
    budgets: InvestigationBudget | None = None,
    now: Callable[[], datetime] = fixed_now,
    max_correction_retries: int = 1,
) -> tuple[Investigator, ScriptedModelProvider]:
    provider = ScriptedModelProvider(script)
    investigator = Investigator(
        provider,
        budgets=budgets,
        now=now,
        ids=SequentialIdFactory(),
        max_correction_retries=max_correction_retries,
    )
    return investigator, provider
