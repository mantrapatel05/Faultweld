"""Closed protocol for one tool-using investigation step."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, TypeAdapter

from app.agent.diagnosis import ModelDiagnosis

TOOL_NAMES = (
    "get_deployment_status",
    "get_readiness_probe",
    "get_recent_changes",
    "get_logs",
)


class ToolCallStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_call"]
    tool: StrictStr
    arguments: dict[str, JsonValue]


class FinalStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["final"]
    diagnosis: ModelDiagnosis


AgentStep = Annotated[ToolCallStep | FinalStep, Field(discriminator="type")]
agent_step_adapter: TypeAdapter[AgentStep] = TypeAdapter(AgentStep)


def step_json_schema() -> dict[str, object]:
    """Describe each allowed step; runtime validation is the authoritative check."""
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "type": {"const": "tool_call"},
                    "tool": {"type": "string", "enum": list(TOOL_NAMES)},
                    "arguments": {"type": "object"},
                },
                "required": ["type", "tool", "arguments"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "final"},
                    "diagnosis": ModelDiagnosis.model_json_schema(),
                },
                "required": ["type", "diagnosis"],
                "additionalProperties": False,
            },
        ]
    }
