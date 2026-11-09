from __future__ import annotations

import json
from decimal import Decimal
from unittest import TestCase

from app.agent.llm.client import ClientResponse
from app.agent.llm.config import LlmConfig
from app.agent.llm.errors import LlmClientError
from app.agent.llm.prompts import render_prompt, request_material
from app.agent.llm.provider import LlmProvider, parse_json_response
from app.agent.provider import ModelEvidence, ModelProviderError, ModelPurpose, ModelRequest
from tests.unit.agent_support import diagnosis_output, snapshot_item


class FakeClient:
    def __init__(self, *steps: ClientResponse | LlmClientError) -> None:
        self.steps = list(steps)
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> ClientResponse:
        self.calls += 1
        self.requests.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, LlmClientError):
            raise step
        return step


def request(*, max_attempts: int = 3, correction_hint: str | None = None) -> ModelRequest:
    item = snapshot_item()
    return ModelRequest(
        purpose=ModelPurpose.DIAGNOSE,
        incident_id="incident-1",
        run_id="run-1",
        evidence=(ModelEvidence(record=item.record, payload=item.payload),),
        max_provider_attempts=max_attempts,
        correction_hint=correction_hint,
    )


def valid_response() -> ClientResponse:
    evidence_id = request().evidence[0].record.evidence_id
    return ClientResponse(
        json.dumps(diagnosis_output([evidence_id])), input_tokens=12, output_tokens=8
    )


class LlmProviderTests(TestCase):
    def test_valid_json_becomes_shared_model_response(self) -> None:
        client = FakeClient(valid_response())
        provider = LlmProvider(LlmConfig(), client)
        response = provider.complete(request())
        self.assertEqual(response.output["is_sufficient"], True)
        self.assertEqual(response.usage.total_tokens, 20)
        self.assertEqual(response.usage.attempts, 1)
        self.assertFalse(response.usage.estimated)

    def test_correction_request_uses_json_object_mode(self) -> None:
        client = FakeClient(valid_response())
        provider = LlmProvider(LlmConfig(), client)
        provider.complete(request(correction_hint="Return all required fields."))
        self.assertEqual(client.requests[0]["schema"], {"type": "json_object"})

    def test_transient_retry_is_bounded_and_counted(self) -> None:
        sleeps: list[float] = []
        client = FakeClient(LlmClientError("timeout"), valid_response())
        provider = LlmProvider(
            LlmConfig(max_retries=3), client, sleep=sleeps.append, random_value=lambda: 0.0
        )
        result = provider.complete(request(max_attempts=2))
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(result.usage.attempts, 2)

    def test_retry_count_never_exceeds_investigator_remaining_budget(self) -> None:
        client = FakeClient(
            LlmClientError("timeout"),
            LlmClientError("timeout"),
            LlmClientError("timeout"),
        )
        provider = LlmProvider(
            LlmConfig(max_retries=4), client, sleep=lambda _delay: None
        )
        with self.assertRaises(ModelProviderError) as raised:
            provider.complete(request(max_attempts=2))
        self.assertEqual(raised.exception.code, "timeout")
        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(client.calls, 2)

    def test_auth_failure_does_not_retry(self) -> None:
        client = FakeClient(LlmClientError("auth_failed"), valid_response())
        provider = LlmProvider(LlmConfig(max_retries=5), client)
        with self.assertRaises(ModelProviderError) as raised:
            provider.complete(request())
        self.assertEqual(raised.exception.code, "auth_failed")
        self.assertEqual(client.calls, 1)

    def test_invalid_schema_is_stable_and_does_not_retry(self) -> None:
        client = FakeClient(ClientResponse('{"unexpected":true}', 1, 1))
        provider = LlmProvider(LlmConfig(max_retries=3), client)
        with self.assertRaises(ModelProviderError) as raised:
            provider.complete(request())
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(client.calls, 1)

    def test_cost_cap_rejects_before_client_call(self) -> None:
        client = FakeClient(valid_response())
        config = LlmConfig(
            input_price_per_million_usd=Decimal("10"),
            output_price_per_million_usd=Decimal("10"),
            max_run_cost_usd=Decimal("0.000001"),
        )
        provider = LlmProvider(config, client)
        with self.assertRaises(ModelProviderError) as raised:
            provider.complete(request())
        self.assertEqual(raised.exception.code, "cost_limit")
        self.assertEqual(client.calls, 0)

    def test_retry_cost_reserves_one_worst_case_budget_per_http_attempt(self) -> None:
        client = FakeClient(LlmClientError("timeout"), valid_response())
        config = LlmConfig(
            input_price_per_million_usd=Decimal("1"),
            output_price_per_million_usd=Decimal("1"),
            max_run_cost_usd=Decimal("0.003"),
            max_retries=3,
        )
        provider = LlmProvider(config, client, sleep=lambda _delay: None)
        with self.assertRaises(ModelProviderError) as raised:
            provider.complete(request())
        self.assertEqual(raised.exception.code, "cost_limit")
        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(client.calls, 1)

    def test_missing_usage_is_estimated(self) -> None:
        client = FakeClient(ClientResponse(valid_response().text, None, None))
        result = LlmProvider(LlmConfig(), client).complete(request())
        self.assertTrue(result.usage.estimated)
        self.assertGreater(result.usage.total_tokens, 0)

    def test_reported_zero_usage_is_not_misclassified_as_missing(self) -> None:
        raw = valid_response()
        client = FakeClient(ClientResponse(raw.text, 0, 0))
        result = LlmProvider(LlmConfig(), client).complete(request())
        self.assertEqual(result.usage.input_tokens, 0)
        self.assertEqual(result.usage.output_tokens, 0)
        self.assertFalse(result.usage.estimated)

    def test_json_parser_rejects_duplicate_keys_and_oversize_responses(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_response('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            parse_json_response(" " * 65_000)

    def test_prompt_contains_only_redacted_allow_list_material(self) -> None:
        item = request().evidence[0]
        request_with_alert = request().model_copy(
            update={"alert_summary": "token=canary-alert"}
        )
        material = request_material(request_with_alert)
        prompt = render_prompt(request_with_alert, {"type": "object"})
        self.assertIn(item.record.evidence_id, prompt)
        self.assertIn("[redacted]", prompt)
        self.assertNotIn("canary-alert", prompt)
        self.assertNotIn("incident-1", prompt)
        self.assertNotIn("run-1", prompt)
        self.assertEqual(
            set(material), {"purpose", "alert_summary", "correction_hint", "evidence"}
        )
