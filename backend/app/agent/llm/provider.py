"""Bounded, validated OpenAI-compatible implementation of the shared provider protocol."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from decimal import Decimal
from threading import Lock
from typing import cast

from pydantic import JsonValue, ValidationError

from app.agent.diagnosis import ModelDiagnosis
from app.agent.llm.client import ClientResponse, LlmClient
from app.agent.llm.config import LlmConfig
from app.agent.llm.errors import LlmClientError
from app.agent.llm.pricing import token_cost_usd
from app.agent.llm.prompts import render_prompt
from app.agent.llm.schemas import normalize_model_output, schema_for
from app.agent.proposal import ModelProposal
from app.agent.protocol import agent_step_adapter
from app.agent.provider import (
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)

MAX_RESPONSE_BYTES = 64_000


def parse_json_response(text: str) -> dict[str, JsonValue]:
    """Extract one bounded JSON object from a model reply.

    Some providers wrap schema-shaped output in Markdown fences or add a short preamble.
    We tolerate those presentation differences, but still reject duplicate keys, malformed
    JSON, and non-object output. The subsequent local Pydantic validation remains strict.
    """

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    decoder = json.JSONDecoder(
        object_pairs_hook=unique_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid constant")),
    )
    # Search top-level object starts. Advancing past each complete object means nested
    # hypothesis/parameter objects are not mistaken for separate answers.
    candidates: list[dict[str, object]] = []
    duplicate_key = False
    index = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError as error:
            duplicate_key = duplicate_key or str(error) == "duplicate JSON key"
            index += 1
            continue
        if isinstance(value, dict):
            candidates.append(value)
            index = _end
        else:
            index = _end
    if len(candidates) != 1:
        if duplicate_key:
            raise ValueError("duplicate_json_key")
        raise ValueError("no_json_object" if not candidates else "multiple_json_objects")
    return cast(dict[str, JsonValue], candidates[0])


class LlmProvider:
    """Calls a configured model endpoint without granting it system authority."""

    provider_id = "openrouter"

    def __init__(
        self,
        config: LlmConfig,
        client: LlmClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._client = client
        self._sleep = sleep
        self._random_value = random_value
        self._reserved_by_run: dict[str, Decimal] = {}
        self._lock = Lock()

    @property
    def model_id(self) -> str:
        return self._config.model_id

    def complete(self, request: ModelRequest) -> ModelResponse:
        schema = schema_for(request.purpose.value)
        prompt = render_prompt(request, schema)
        # UTF-8 byte length is a conservative token upper bound for the cost reservation.
        estimated_input = max(1, len(prompt.encode("utf-8")))
        reservation = token_cost_usd(
            input_tokens=estimated_input,
            output_tokens=self._config.max_output_tokens,
            input_price_per_million_usd=self._config.input_price_per_million_usd,
            output_price_per_million_usd=self._config.output_price_per_million_usd,
        )
        attempt_limit = min(request.max_provider_attempts, self._config.max_retries + 1)
        attempts = 0
        failed_attempts = 0
        while attempts < attempt_limit:
            if not self._reserve(request.run_id, reservation):
                raise ModelProviderError(
                    "cost_limit",
                    "run cost limit reached",
                    attempts=attempts,
                    tokens_used=failed_attempts
                    * (estimated_input + self._config.max_output_tokens),
                )
            attempts += 1
            raw: ClientResponse | None = None
            try:
                raw = self._client.complete(
                    prompt=prompt,
                    schema=(
                        {"type": "json_object"}
                        if request.correction_hint is not None
                        or request.purpose.value == "investigate_step"
                        else schema
                    ),
                    model=self._config.model_id,
                    max_output_tokens=self._config.max_output_tokens,
                    temperature=self._config.temperature,
                    timeout_seconds=self._config.timeout_seconds,
                )
                try:
                    parsed = parse_json_response(raw.text)
                except (ValueError, TypeError) as error:
                    raise ModelProviderError(
                        "invalid_response",
                        "model response was not a JSON object",
                        attempts=attempts,
                        tokens_used=self._response_tokens(raw, estimated_input),
                        status_code=raw.status_code,
                        failure_stage="json_parse",
                        failure_details=(str(error),),
                    ) from error
                try:
                    if request.purpose.value == "investigate_step":
                        agent_step_adapter.validate_python(parsed)
                        output = parsed
                    else:
                        output = normalize_model_output(request.purpose.value, parsed)
                        output_model = (
                            ModelDiagnosis
                            if request.purpose.value == "diagnose"
                            else ModelProposal
                        )
                        output_model.model_validate(output)
                except ValidationError as error:
                    response_keys = tuple(
                        sorted(
                            key
                            for key in parsed
                            if key.isidentifier() and len(key) <= 32
                        )[:8]
                    )
                    raise ModelProviderError(
                        "invalid_response",
                        "model response did not match its schema",
                        attempts=attempts,
                        tokens_used=self._response_tokens(raw, estimated_input),
                        status_code=raw.status_code,
                        failure_stage="schema_validation",
                        failure_details=tuple(
                            ".".join(str(part) for part in issue["loc"])
                            + ":"
                            + str(issue["type"])
                            for issue in error.errors(include_input=False)
                        )
                        + ("response_keys=" + ",".join(response_keys),),
                    ) from error
                estimated = raw.input_tokens is None or raw.output_tokens is None
                input_tokens = (
                    estimated_input if raw.input_tokens is None else raw.input_tokens
                )
                output_tokens = (
                    self._config.max_output_tokens
                    if raw.output_tokens is None
                    else raw.output_tokens
                )
                cost = token_cost_usd(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_price_per_million_usd=self._config.input_price_per_million_usd,
                    output_price_per_million_usd=self._config.output_price_per_million_usd,
                )
                self._settle(request.run_id, reservation, cost)
                unreported_tokens = failed_attempts * (
                    estimated_input + self._config.max_output_tokens
                )
                charged_cost = cost + reservation * failed_attempts
                return ModelResponse(
                    output=output,
                    usage=ModelUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=float(charged_cost),
                        estimated=estimated or failed_attempts > 0,
                        attempts=attempts,
                        unreported_tokens=unreported_tokens,
                    ),
                )
            except LlmClientError as error:
                retryable = error.code in ("timeout", "rate_limited", "unavailable")
                if retryable and attempts < attempt_limit:
                    failed_attempts += 1
                    delay = min(8.0, 0.5 * (2 ** (attempts - 1)))
                    self._sleep(delay * (0.5 + self._random_value()))
                    continue
                # A failed response may have incurred the full reserved cost. Keep it reserved.
                raise ModelProviderError(
                    error.code,
                    "model request failed",
                    attempts=attempts,
                    tokens_used=(failed_attempts + 1)
                    * (estimated_input + self._config.max_output_tokens),
                    status_code=error.status_code,
                    failure_stage=(
                        "client_response"
                        if error.code == "invalid_response"
                        else None
                    ),
                ) from None
            except (ValueError, TypeError):
                input_tokens = (
                    estimated_input if raw is None or raw.input_tokens is None else raw.input_tokens
                )
                output_tokens = (
                    self._config.max_output_tokens
                    if raw is None or raw.output_tokens is None
                    else raw.output_tokens
                )
                raise ModelProviderError(
                    "invalid_response",
                    "model response did not match the output schema",
                    attempts=attempts,
                    tokens_used=input_tokens + output_tokens,
                    failure_stage="json_parse",
                ) from None
        raise AssertionError("bounded provider loop ended without a result")

    def _reserve(self, run_id: str, amount: Decimal) -> bool:
        with self._lock:
            reserved = self._reserved_by_run.get(run_id, Decimal("0"))
            if reserved + amount > self._config.max_run_cost_usd:
                return False
            self._reserved_by_run[run_id] = reserved + amount
            return True

    def _response_tokens(self, response: ClientResponse, estimated_input: int) -> int:
        input_tokens = (
            estimated_input if response.input_tokens is None else response.input_tokens
        )
        output_tokens = (
            self._config.max_output_tokens
            if response.output_tokens is None
            else response.output_tokens
        )
        return input_tokens + output_tokens

    def _settle(self, run_id: str, reserved_cost: Decimal, actual_cost: Decimal) -> None:
        with self._lock:
            reserved = self._reserved_by_run.get(run_id, Decimal("0"))
            self._reserved_by_run[run_id] = max(
                Decimal("0"), reserved - reserved_cost + actual_cost
            )
