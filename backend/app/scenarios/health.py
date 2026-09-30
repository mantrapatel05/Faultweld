from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.scenarios.models import ScenarioDefinition

HealthState = Literal["healthy", "faulted"]


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


def classify_health(
    scenario: ScenarioDefinition, observation: HealthObservation
) -> HealthState:
    if (
        observation.available_replicas >= scenario.health.healthy_replicas
        and observation.ready_replicas >= scenario.health.healthy_replicas
    ):
        return "healthy"
    return "faulted"
