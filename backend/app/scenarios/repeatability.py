"""Repeatability gate for scenario fixtures.

This module is pure: it compares observations against a fixture's declared
tolerances and produces a deterministic verdict. It performs no cluster calls,
so the same comparison logic runs in unit tests against a simulated cluster and
against a live ``kind-faultweld`` cluster.

The gate never downgrades a mismatch into a pass. An observation that sits
between the declared healthy and faulted bounds is recorded as
``indeterminate`` and fails the cycle, because an unstable probe is evidence
that the fixture is not yet reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.scenarios.health import HealthObservation, MatchState, observed_state
from app.scenarios.models import ScenarioDefinition


class RunPhase(StrEnum):
    BASELINE = "baseline"
    FAULTED = "faulted"
    RESET = "reset"
    RESET_AGAIN = "reset_again"


EXPECTED_PHASES: tuple[RunPhase, ...] = (
    RunPhase.BASELINE,
    RunPhase.FAULTED,
    RunPhase.RESET,
    RunPhase.RESET_AGAIN,
)

PHASE_EXPECTATION: dict[RunPhase, MatchState] = {
    RunPhase.BASELINE: MatchState.HEALTHY,
    RunPhase.FAULTED: MatchState.FAULTED,
    RunPhase.RESET: MatchState.HEALTHY,
    RunPhase.RESET_AGAIN: MatchState.HEALTHY,
}

PHASE_DESCRIPTION: dict[RunPhase, str] = {
    RunPhase.BASELINE: "baseline before injection",
    RunPhase.FAULTED: "state immediately after injection",
    RunPhase.RESET: "state after the first reset",
    RunPhase.RESET_AGAIN: "state after a repeated reset (idempotency)",
}


class PhaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: RunPhase
    expected: MatchState
    observed: MatchState
    observation: HealthObservation

    @property
    def matched(self) -> bool:
        return self.observed is self.expected

    def failure_reason(self) -> str | None:
        if self.matched:
            return None
        return (
            f"{self.phase.value} expected {self.expected.value} but observed "
            f"{self.observed.value} "
            f"(desired={self.observation.desired_replicas}, "
            f"available={self.observation.available_replicas}, "
            f"ready={self.observation.ready_replicas})"
        )


class CycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    fixture_version: str
    cycle: int = Field(ge=1)
    phases: tuple[PhaseResult, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            reason
            for phase in self.phases
            if (reason := phase.failure_reason()) is not None
        )

    @property
    def passed(self) -> bool:
        return not self.failures


class ScenarioVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    fixture_version: str
    repeats: int = Field(ge=1)
    cycles_passed: int = Field(ge=0)
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


class RepeatabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    environment_id: str
    namespace: str
    target_deployment: str
    repeats: int = Field(ge=1)
    verdicts: tuple[ScenarioVerdict, ...]

    @property
    def passed(self) -> bool:
        return all(verdict.passed for verdict in self.verdicts)


def compare_phase(
    scenario: ScenarioDefinition, phase: RunPhase, observation: HealthObservation
) -> PhaseResult:
    return PhaseResult(
        phase=phase,
        expected=PHASE_EXPECTATION[phase],
        observed=observed_state(scenario, observation),
        observation=observation,
    )


def build_cycle(
    scenario: ScenarioDefinition,
    cycle: int,
    observations: Sequence[HealthObservation],
) -> CycleResult:
    if len(observations) != len(EXPECTED_PHASES):
        raise ValueError(
            f"cycle {cycle} for {scenario.scenario_id} needs "
            f"{len(EXPECTED_PHASES)} observations, received {len(observations)}"
        )
    return CycleResult(
        scenario_id=scenario.scenario_id,
        fixture_version=scenario.version,
        cycle=cycle,
        phases=tuple(
            compare_phase(scenario, phase, observation)
            for phase, observation in zip(EXPECTED_PHASES, observations, strict=True)
        ),
    )


def summarize(scenario: ScenarioDefinition, cycles: Sequence[CycleResult]) -> ScenarioVerdict:
    if not cycles:
        raise ValueError(f"{scenario.scenario_id} requires at least one cycle")
    failures = tuple(
        f"cycle {result.cycle}: {reason}"
        for result in cycles
        for reason in result.failures
    )
    return ScenarioVerdict(
        scenario_id=scenario.scenario_id,
        fixture_version=scenario.version,
        repeats=len(cycles),
        cycles_passed=sum(1 for result in cycles if result.passed),
        failures=failures,
    )


def build_report(
    scenarios: Sequence[ScenarioDefinition],
    verdicts: Sequence[ScenarioVerdict],
    repeats: int,
    schema_version: str,
) -> RepeatabilityReport:
    if not scenarios:
        raise ValueError("a repeatability report needs at least one scenario")
    first = scenarios[0]
    for scenario, verdict in zip(scenarios, verdicts, strict=True):
        if scenario.scenario_id != verdict.scenario_id:
            raise ValueError(
                f"verdict {verdict.scenario_id} does not match scenario {scenario.scenario_id}"
            )
    return RepeatabilityReport(
        schema_version=schema_version,
        environment_id=first.environment_id,
        namespace=first.namespace,
        target_deployment=first.target_deployment,
        repeats=repeats,
        verdicts=tuple(verdicts),
    )
