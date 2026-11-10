"""Fake model providers for live-eval tests. Deterministic, no network, no key."""

from __future__ import annotations

from pydantic import JsonValue

from app.agent.provider import (
    ModelProvider,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    scripted_response,
)
from app.evals.live import LiveCase, ProviderFactory


class FakeLiveModel:
    """Behaves like a well-behaved model: scale to 1 when asked to remediate."""

    def __init__(self, *, cost_usd: float = 0.0, action: str | None = "scale_deployment",
                 replicas: int = 1) -> None:
        self.calls = 0
        self._step_calls = 0
        self.run_ids: list[str] = []
        self._cost = cost_usd
        self._action = action
        self._replicas = replicas

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if request.run_id not in self.run_ids:
            self.run_ids.append(request.run_id)
        evidence_id = request.evidence_ids[0]
        diagnosis: dict[str, JsonValue] = {
            "hypotheses": [
                {
                    "statement": "replica shortfall",
                    "confidence": 0.9,
                    "supporting_evidence_ids": [evidence_id],
                    "contradicting_evidence_ids": [],
                }
            ],
            "missing_evidence": [],
            "confidence": 0.9,
            "is_sufficient": True,
        }
        if request.purpose is ModelPurpose.INVESTIGATE_STEP:
            self._step_calls += 1
            if self._step_calls == 1:
                return scripted_response(
                    {"type": "tool_call", "tool": "get_deployment_status", "arguments": {}},
                    cost_usd=self._cost,
                )
            return scripted_response(
                {"type": "final", "diagnosis": diagnosis}, cost_usd=self._cost
            )
        if request.purpose is ModelPurpose.DIAGNOSE:
            return scripted_response(diagnosis, cost_usd=self._cost)
        if self._action is None:
            output: dict[str, JsonValue] = {
                "action_type": None,
                "parameters": {},
                "rationale": "none",
                "evidence_ids": [],
            }
        else:
            output = {
                "action_type": self._action,
                "parameters": {"replicas": self._replicas} if self._action == "scale_deployment"
                else {},
                "rationale": "scripted",
                "evidence_ids": [evidence_id],
            }
        return scripted_response(output, cost_usd=self._cost)


def fake_factory(model: FakeLiveModel | None = None) -> ProviderFactory:
    shared = model

    def build(case: LiveCase, repeat: int) -> ModelProvider:
        if shared is not None:
            return shared
        # Escalate cases the way a careful model would: no action on non-remediable ones.
        action = "scale_deployment" if case.scenario_id == "healthy-replica-loss" else None
        return FakeLiveModel(action=action)

    return build
