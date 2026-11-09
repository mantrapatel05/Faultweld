"""Bounded model/read-only-tool loop. Every non-final stop escalates."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue, ValidationError

from app.agent.protocol import FinalStep, agent_step_adapter
from app.agent.provider import (
    ModelEvidence,
    ModelProvider,
    ModelProviderError,
    ModelPurpose,
    ModelRequest,
)
from app.agent.tools.executor import ToolExecutor, ToolResultError
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext, EvidenceItem


@dataclass(frozen=True)
class LoopBudget:
    max_model_calls: int = 8
    max_tool_calls: int = 6
    max_steps: int = 12
    max_tokens: int = 8_000
    max_wall_clock_seconds: float = 120.0
    max_consecutive_invalid_steps: int = 2
    repeated_tool_call_limit: int = 2

    def __post_init__(self) -> None:
        if min(self.max_model_calls, self.max_tool_calls, self.max_tokens) < 0:
            raise ValueError("call and token budgets must not be negative")
        if min(
            self.max_steps,
            self.max_consecutive_invalid_steps,
            self.repeated_tool_call_limit,
        ) < 1 or self.max_wall_clock_seconds < 0:
            raise ValueError("step limits must be positive and wall-clock limit nonnegative")


@dataclass(frozen=True)
class LoopResult:
    diagnosis_output: dict[str, JsonValue] | None
    evidence: tuple[EvidenceItem, ...]
    termination: str
    model_calls: int
    tool_calls: int
    tokens_used: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        executor: ToolExecutor,
        *,
        budget: LoopBudget | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._budget = budget or LoopBudget()
        self._now = now

    def run(
        self,
        *,
        context: EvidenceContext,
        initial_evidence: tuple[EvidenceItem, ...] = (),
        alert_summary: str | None = None,
    ) -> LoopResult:
        started = self._now()
        items = list(initial_evidence)
        calls = tools = tokens = steps = invalid = 0
        correction_hint: str | None = None
        seen_calls: dict[str, int] = {}

        while steps < self._budget.max_steps:
            elapsed = (self._now() - started).total_seconds()
            if elapsed >= self._budget.max_wall_clock_seconds:
                return self._result(items, "wall_clock_exhausted", calls, tools, tokens)
            if calls >= self._budget.max_model_calls:
                return self._result(items, "model_budget_exhausted", calls, tools, tokens)
            if tokens >= self._budget.max_tokens:
                return self._result(items, "token_budget_exhausted", calls, tools, tokens)

            request = ModelRequest(
                purpose=ModelPurpose.INVESTIGATE_STEP,
                incident_id=context.incident_id,
                run_id=context.run_id,
                evidence=tuple(
                    ModelEvidence(record=item.record, payload=item.payload) for item in items
                ),
                alert_summary=alert_summary,
                correction_hint=correction_hint,
                max_provider_attempts=max(1, self._budget.max_model_calls - calls),
            )
            steps += 1
            try:
                response = self._provider.complete(request)
            except ModelProviderError as error:
                calls += error.attempts
                tokens += error.tokens_used
                if calls > self._budget.max_model_calls:
                    return self._result(items, "model_budget_exhausted", calls, tools, tokens)
                if tokens > self._budget.max_tokens:
                    return self._result(items, "token_budget_exhausted", calls, tools, tokens)
                if error.code == "invalid_response" and error.failure_stage in {
                    "json_parse",
                    "schema_validation",
                }:
                    invalid += 1
                    if invalid >= self._budget.max_consecutive_invalid_steps:
                        return self._result(items, "invalid_step_limit", calls, tools, tokens)
                    correction_hint = (
                        "The previous step failed local validation. Return one complete step "
                        "using the requested step schema."
                    )
                    continue
                return self._result(
                    items, f"provider_error:{error.code}", calls, tools, tokens
                )

            calls += response.usage.attempts
            tokens += response.usage.total_tokens
            if calls > self._budget.max_model_calls:
                return self._result(items, "model_budget_exhausted", calls, tools, tokens)
            if tokens > self._budget.max_tokens:
                return self._result(items, "token_budget_exhausted", calls, tools, tokens)
            try:
                step = agent_step_adapter.validate_python(response.output)
            except ValidationError:
                invalid += 1
                if invalid >= self._budget.max_consecutive_invalid_steps:
                    return self._result(items, "invalid_step_limit", calls, tools, tokens)
                correction_hint = (
                    "The previous step failed local validation. Return one complete step "
                    "using the requested step schema."
                )
                continue

            correction_hint = None
            if isinstance(step, FinalStep):
                return LoopResult(
                    diagnosis_output=step.diagnosis.model_dump(mode="json"),
                    evidence=tuple(items),
                    termination="final_received",
                    model_calls=calls,
                    tool_calls=tools,
                    tokens_used=tokens,
                )

            if step.tool not in self._executor.registry.names:
                invalid += 1
                if invalid >= self._budget.max_consecutive_invalid_steps:
                    return self._result(items, "unknown_tool", calls, tools, tokens)
                correction_hint = "Unknown tool. Choose one of the four listed read-only tools."
                continue

            signature = hashlib.sha256(
                canonical_json({"tool": step.tool, "arguments": step.arguments}).encode("utf-8")
            ).hexdigest()
            seen_calls[signature] = seen_calls.get(signature, 0) + 1
            if seen_calls[signature] > self._budget.repeated_tool_call_limit:
                return self._result(items, "loop_detected", calls, tools, tokens)
            if tools >= self._budget.max_tool_calls:
                return self._result(items, "tool_budget_exhausted", calls, tools, tokens)

            tools += 1
            try:
                item = self._executor.execute(step.tool, step.arguments)
            except ToolResultError as error:
                invalid += 1
                if invalid >= self._budget.max_consecutive_invalid_steps:
                    return self._result(items, error.reason, calls, tools, tokens)
                correction_hint = (
                    "The tool call arguments were invalid. Choose a listed read-only tool and "
                    "supply only its required arguments."
                )
                continue
            items.append(item)
            invalid = 0

        return self._result(items, "max_steps_exhausted", calls, tools, tokens)

    @staticmethod
    def _result(
        items: list[EvidenceItem], reason: str, calls: int, tools: int, tokens: int
    ) -> LoopResult:
        return LoopResult(None, tuple(items), reason, calls, tools, tokens)
