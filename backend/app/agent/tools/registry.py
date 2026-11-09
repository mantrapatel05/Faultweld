"""Closed registry of read-only tools and their strict argument schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.agent.protocol import TOOL_NAMES

MAX_RECENT_CHANGES = 10
MAX_LOG_LINES = 20
ArgsModel = TypeVar("ArgsModel", bound=BaseModel)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecentChangesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: StrictInt = Field(ge=1, le=MAX_RECENT_CHANGES)


class LogsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lines: StrictInt = Field(ge=1, le=MAX_LOG_LINES)


ARGUMENT_MODELS: Mapping[str, type[BaseModel]] = {
    "get_deployment_status": NoArguments,
    "get_readiness_probe": NoArguments,
    "get_recent_changes": RecentChangesArguments,
    "get_logs": LogsArguments,
}


class ToolArgumentsError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolRegistry:
    """Validates names and arguments against exactly four registered tools."""

    def __init__(self) -> None:
        if set(ARGUMENT_MODELS) != set(TOOL_NAMES):
            raise RuntimeError("tool registry does not match the closed tool-name list")
        self._models = dict(ARGUMENT_MODELS)

    @property
    def names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    def parse_arguments(self, name: str, arguments: object) -> BaseModel:
        if name not in self._models:
            raise ToolArgumentsError("unknown_tool")
        if not isinstance(arguments, dict):
            raise ToolArgumentsError("invalid_tool_arguments")
        try:
            return self._models[name].model_validate(arguments)
        except ValidationError as error:
            raise ToolArgumentsError("invalid_tool_arguments") from error
