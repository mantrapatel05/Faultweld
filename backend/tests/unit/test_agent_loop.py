from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from unittest import TestCase

from pydantic import JsonValue

from app.agent.loop import AgentLoop, LoopBudget
from app.agent.provider import ModelPurpose, ModelResponse, ScriptedModelProvider, ScriptStep
from app.agent.tools.executor import ToolExecutor
from app.sim.cluster import SimCluster

from .agent_support import (
    CONTEXT,
    FIXED_NOW,
    SequenceClock,
    diagnosis_output,
    response,
    snapshot_item,
)


def tool_step(
    tool: str, arguments: dict[str, JsonValue] | None = None
) -> ModelResponse:
    return response({"type": "tool_call", "tool": tool, "arguments": arguments or {}})


def final_step(evidence_id: str) -> ModelResponse:
    return response(
        {"type": "final", "diagnosis": diagnosis_output([evidence_id])},
        input_tokens=3,
        output_tokens=2,
    )


def build_loop(
    script: Sequence[ScriptStep],
    *,
    budget: LoopBudget | None = None,
    source: SimCluster | None = None,
    clock: Callable[[], datetime] = lambda: FIXED_NOW,
) -> tuple[AgentLoop, ScriptedModelProvider, SimCluster]:
    cluster = source or SimCluster()
    provider = ScriptedModelProvider(script)
    executor = ToolExecutor(cluster.source, CONTEXT, now=clock)
    return AgentLoop(provider, executor, budget=budget, now=clock), provider, cluster


class AgentLoopTests(TestCase):
    def test_read_tool_then_final_returns_typed_diagnosis_and_counts(self) -> None:
        evidence = snapshot_item()
        loop, provider, _ = build_loop(
            [tool_step("get_deployment_status"), final_step(evidence.record.evidence_id)]
        )

        result = loop.run(context=CONTEXT, initial_evidence=(evidence,))

        self.assertEqual(result.termination, "final_received")
        self.assertEqual((result.model_calls, result.tool_calls, result.tokens_used), (2, 1, 5))
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(provider.requests[0].purpose, ModelPurpose.INVESTIGATE_STEP)
        self.assertIn(result.evidence[1].record.evidence_id, provider.requests[1].evidence_ids)
        diagnosis = result.diagnosis_output
        if diagnosis is None:
            self.fail("a final step should return a diagnosis")
        self.assertEqual(diagnosis["is_sufficient"], True)

    def test_zero_model_budget_makes_no_provider_call(self) -> None:
        loop, provider, _ = build_loop(
            [], budget=LoopBudget(max_model_calls=0)
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "model_budget_exhausted")
        self.assertEqual(provider.calls, 0)

    def test_tool_budget_stops_before_calling_tool_over_limit(self) -> None:
        loop, provider, _ = build_loop(
            [tool_step("get_deployment_status"), final_step(snapshot_item().record.evidence_id)],
            budget=LoopBudget(max_tool_calls=0),
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "tool_budget_exhausted")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(provider.calls, 1)

    def test_repeated_identical_tool_calls_terminate_at_limit_plus_one(self) -> None:
        repeated = [tool_step("get_deployment_status") for _ in range(3)]
        loop, provider, _ = build_loop(
            repeated,
            budget=LoopBudget(repeated_tool_call_limit=2),
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "loop_detected")
        self.assertEqual((result.model_calls, result.tool_calls), (3, 2))
        self.assertEqual(provider.calls, 3)

    def test_token_budget_is_checked_before_provider_call(self) -> None:
        loop, provider, _ = build_loop(
            [response({"type": "final", "diagnosis": diagnosis_output(["ev-missing"])})],
            budget=LoopBudget(max_tokens=0),
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "token_budget_exhausted")
        self.assertEqual(provider.calls, 0)

    def test_wall_clock_budget_is_checked_before_provider_call(self) -> None:
        clock = SequenceClock([FIXED_NOW, FIXED_NOW + timedelta(seconds=2)])
        loop, provider, _ = build_loop(
            [],
            budget=LoopBudget(max_wall_clock_seconds=1),
            clock=clock,
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "wall_clock_exhausted")
        self.assertEqual(provider.calls, 0)

    def test_max_step_limit_is_a_named_termination(self) -> None:
        loop, _provider, _ = build_loop(
            [tool_step("get_deployment_status"), tool_step("get_readiness_probe")],
            budget=LoopBudget(max_steps=1),
        )
        result = loop.run(context=CONTEXT)
        self.assertEqual(result.termination, "max_steps_exhausted")
        self.assertEqual(result.tool_calls, 1)

    def test_same_fixed_inputs_produce_same_tool_evidence_id(self) -> None:
        outputs = []
        for _ in range(2):
            loop, _provider, _cluster = build_loop([tool_step("get_logs", {"lines": 1})])
            outputs.append(loop.run(context=CONTEXT).evidence[0].record.evidence_id)
        self.assertEqual(outputs[0], outputs[1])
