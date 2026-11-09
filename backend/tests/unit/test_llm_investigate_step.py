from __future__ import annotations

import json
from unittest import TestCase

from app.agent.llm.client import ClientResponse
from app.agent.llm.config import LlmConfig
from app.agent.llm.prompts import PROMPT_VERSION, render_prompt
from app.agent.llm.provider import LlmProvider
from app.agent.llm.schemas import schema_for
from app.agent.provider import ModelPurpose, ModelRequest
from tests.unit.agent_support import diagnosis_output


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.schema: object | None = None

    def complete(self, **kwargs: object) -> ClientResponse:
        self.calls += 1
        self.schema = kwargs["schema"]
        return ClientResponse(self.text, input_tokens=4, output_tokens=3)


def request() -> ModelRequest:
    return ModelRequest(
        purpose=ModelPurpose.INVESTIGATE_STEP,
        incident_id="incident-1",
        run_id="run-1",
        evidence=(),
    )


class LlmInvestigateStepTests(TestCase):
    def test_step_prompt_requests_a_read_before_final_diagnosis(self) -> None:
        rendered = render_prompt(request(), schema_for("investigate_step"))
        self.assertEqual(PROMPT_VERSION, "faultweld-agentic-3")
        self.assertIn("call at least one relevant read-only tool", rendered)

    def test_valid_tool_call_is_returned_as_step_data(self) -> None:
        client = FakeClient(
            json.dumps(
                {
                    "type": "tool_call",
                    "tool": "get_deployment_status",
                    "arguments": {},
                }
            )
        )
        result = LlmProvider(LlmConfig(), client).complete(request())

        self.assertEqual(result.output["type"], "tool_call")
        self.assertEqual(result.output["tool"], "get_deployment_status")
        self.assertEqual(client.schema, {"type": "json_object"})
        self.assertEqual(result.usage.total_tokens, 7)

    def test_valid_final_diagnosis_uses_local_step_validation(self) -> None:
        client = FakeClient(
            json.dumps({"type": "final", "diagnosis": diagnosis_output([])})
        )
        result = LlmProvider(LlmConfig(), client).complete(request())
        self.assertEqual(result.output["type"], "final")

    def test_unknown_tool_is_preserved_for_bounded_loop_rejection(self) -> None:
        client = FakeClient(
            json.dumps({"type": "tool_call", "tool": "delete_cluster", "arguments": {}})
        )
        result = LlmProvider(LlmConfig(), client).complete(request())
        self.assertEqual(result.output["tool"], "delete_cluster")
        self.assertEqual(client.calls, 1)
