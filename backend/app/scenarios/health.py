from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.scenarios.models import ScenarioDefinition


class HealthState(StrEnum):
    HEALTHY = "healthy"
    FAULTED = "faulted"


class MatchState(StrEnum):
    """Three-state view used by the repeatability gate.

    ``classify_health`` collapses this into two states for operator output. The
    repeatability gate keeps ``INDETERMINATE`` so that a partially converged
    observation is reported as an unstable probe instead of being silently
    accepted as a fault.
    """

    HEALTHY = "healthy"
    FAULTED = "faulted"
    INDETERMINATE = "indeterminate"


class HealthObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    desired_replicas: int = Field(ge=0)
    available_replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)


def observe_deployment(payload: Mapping[str, Any]) -> HealthObservation:
    spec = payload.get("spec", {})
    status = payload.get("status", {})
    if not isinstance(spec, Mapping) or not isinstance(status, Mapping):
        raise ValueError("deployment payload must contain spec/status mappings")
    return HealthObservation(
        desired_replicas=spec.get("replicas", 0),
        available_replicas=status.get("availableReplicas", 0),
        ready_replicas=status.get("readyReplicas", 0),
    )


def observed_state(scenario: ScenarioDefinition, observation: HealthObservation) -> MatchState:
    """Compare an observation against the fixture's declared replica tolerances."""
    healthy = scenario.health.healthy_replicas
    faulted = scenario.health.faulted_replicas
    if (
        observation.available_replicas >= healthy
        and observation.ready_replicas >= healthy
    ):
        return MatchState.HEALTHY
    if (
        observation.available_replicas <= faulted
        and observation.ready_replicas <= faulted
    ):
        return MatchState.FAULTED
    return MatchState.INDETERMINATE


def classify_health(scenario: ScenarioDefinition, observation: HealthObservation) -> HealthState:
    if observed_state(scenario, observation) is MatchState.HEALTHY:
        return HealthState.HEALTHY
    return HealthState.FAULTED
