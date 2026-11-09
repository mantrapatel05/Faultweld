"""Model provider abstraction and a deterministic scripted provider.

A provider receives a structured request -- purpose, identifiers, raw evidence records and
their bounded payloads -- and returns JSON plus usage. There is no prompt text and no tool
call. The scripted provider is the only implementation in this commit: it replays
pre-written steps and never invents a reply.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.domain.schemas import Evidence, NonEmptyId

ProviderErrorCode = Literal[
    "timeout",
    "rate_limited",
    "unavailable",
    "invalid_response",
    "script_exhausted",
    "auth_failed",
    "cost_limit",
]
ProviderFailureStage = Literal[
    "client_response", "json_parse", "schema_validation", "cost_guard"
]


class ModelProviderError(RuntimeError):
    """A provider failure with a stable, machine-readable code."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        attempts: int = 1,
        tokens_used: int = 0,
        status_code: int | None = None,
        failure_stage: ProviderFailureStage | None = None,
        failure_details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code: ProviderErrorCode = code
        self.attempts = attempts
        self.tokens_used = tokens_used
        self.status_code = status_code
        self.failure_stage = failure_stage
        self.failure_details = failure_details


class ModelPurpose(StrEnum):
    DIAGNOSE = "diagnose"
    PROPOSE = "propose"


class ModelEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Evidence
    payload: dict[str, JsonValue]


class ModelRequest(BaseModel):
    """Everything the model is given. It contains no prompt and no caller-chosen text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: ModelPurpose
    incident_id: NonEmptyId
    run_id: NonEmptyId
    evidence: tuple[ModelEvidence, ...]
    alert_summary: str | None = Field(default=None, max_length=1000)
    correction_hint: str | None = Field(default=None, max_length=300)
    max_provider_attempts: int = Field(default=1, ge=1)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.record.evidence_id for item in self.evidence)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    estimated: bool = False
    attempts: int = Field(default=1, ge=1)
    unreported_tokens: int = Field(default=0, ge=0)

    @field_validator("cost_usd")
    @classmethod
    def validate_finite_cost(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cost_usd must be finite")
        return value

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.unreported_tokens


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output: dict[str, JsonValue]
    usage: ModelUsage = ModelUsage()


class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


type ScriptStep = ModelResponse | ModelProviderError


def scripted_response(
    output: dict[str, JsonValue],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> ModelResponse:
    usage = ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)
    return ModelResponse(output=output, usage=usage)


class ScriptedModelProvider:
    """Returns pre-written steps in order and records every request it receives.

    Running past the end of the script raises ``script_exhausted`` instead of inventing a
    response, so a test that causes an unexpected extra call fails deterministically.
    """

    provider_id = "scripted"
    model_id = "scripted-v1"

    def __init__(self, script: Sequence[ScriptStep] = ()) -> None:
        self._script: deque[ScriptStep] = deque(script)
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    @property
    def calls(self) -> int:
        return len(self._requests)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self._requests.append(request)
        if not self._script:
            raise ModelProviderError("script_exhausted", "the scripted provider has no more steps")
        step = self._script.popleft()
        if isinstance(step, ModelProviderError):
            raise step
        return step
