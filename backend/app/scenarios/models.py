from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIO_SCHEMA_VERSION = "0.2"
ScenarioFaultKind = Literal["scale", "readiness", "delete_pod"]


class ScenarioPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ScenarioFault(ScenarioPart):
    kind: ScenarioFaultKind
    replicas: int | None = Field(default=None, ge=0, le=1)
    path: str | None = None
    selector: str | None = None

    @model_validator(mode="after")
    def validate_kind_arguments(self) -> ScenarioFault:
        expected_fields = {
            "scale": self.replicas is not None,
            "readiness": self.path is not None,
            "delete_pod": self.selector is not None,
        }
        if not expected_fields[self.kind]:
            raise ValueError(f"fault kind {self.kind!r} requires its matching argument")
        return self


class ScenarioAlert(ScenarioPart):
    alert_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=500)


class ScenarioHealth(ScenarioPart):
    healthy_replicas: int = Field(ge=1, le=1)
    faulted_replicas: int = Field(ge=0, le=0)


class ScenarioCleanup(ScenarioPart):
    replicas: int | None = Field(default=None, ge=0, le=1)
    readiness_path: str | None = None


class ScenarioDefinition(ScenarioPart):
    schema_version: Literal["0.2"]
    scenario_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]*$")
    title: str = Field(min_length=1, max_length=200)
    environment_id: Literal["kind-faultweld"]
    namespace: Literal["faultweld-demo"]
    target_deployment: Literal["faultweld-sample"]
    initial_replicas: Literal[1]
    fault: ScenarioFault
    alert: ScenarioAlert
    health: ScenarioHealth
    cleanup: ScenarioCleanup
