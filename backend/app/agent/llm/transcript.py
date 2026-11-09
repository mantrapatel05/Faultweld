"""Redacted, offline-replayable records of provider calls."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from app.agent.llm.prompts import PROMPT_VERSION, render_prompt, request_material
from app.agent.llm.schemas import SCHEMA_VERSION, schema_for
from app.agent.provider import ModelProvider, ModelProviderError, ModelRequest, ModelResponse
from app.evidence.redaction import redact_text

TranscriptErrorCode = Literal[
    "timeout",
    "rate_limited",
    "unavailable",
    "invalid_response",
    "script_exhausted",
    "auth_failed",
    "cost_limit",
]


def _redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return cast(JsonValue, redact_text(value))
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def request_digest(request: ModelRequest) -> str:
    return request_signature(request, model_id="recorded", parameters={})[0]


def request_signature(
    request: ModelRequest,
    *,
    model_id: str,
    parameters: dict[str, JsonValue],
) -> tuple[str, str]:
    schema = schema_for(request.purpose.value)
    prompt = render_prompt(request, schema)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    signature = {
        "model_id": model_id,
        "parameters": parameters,
        "prompt_digest": prompt_hash,
        "schema_version": SCHEMA_VERSION,
    }
    return hashlib.sha256(_canonical(signature)).hexdigest(), prompt_hash


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript_version: Literal["0.1"] = "0.1"
    provider: str
    model_id: str
    parameters: dict[str, JsonValue]
    prompt_version: str
    schema_version: str
    prompt_digest: str
    request_digest: str
    request: dict[str, JsonValue]
    response: dict[str, JsonValue] | None = None
    usage: dict[str, JsonValue] | None = None
    response_digest: str | None = None
    error_code: TranscriptErrorCode | None = None
    failure_stage: str | None = None
    failure_details: tuple[str, ...] = ()
    http_status: int | None = None
    attempts: int = 0
    tokens_used: int = 0
    latency_ms: int = 0
    recorded_at: datetime


class RecordingProvider:
    """Wrap a provider and persist each result for deterministic offline replay."""

    def __init__(
        self,
        provider: ModelProvider,
        directory: Path,
        *,
        provider_id: str,
        model_id: str,
        parameters: dict[str, JsonValue] | None = None,
    ) -> None:
        self._provider = provider
        self._directory = directory
        self._provider_id = provider_id
        self._model_id = model_id
        self._parameters = parameters or {}
        self._sequence = 0
        directory.mkdir(parents=True, exist_ok=True)

    def complete(self, request: ModelRequest) -> ModelResponse:
        material = _redact(cast(JsonValue, request_material(request)))
        digest, prompt_hash = request_signature(
            request, model_id=self._model_id, parameters=self._parameters
        )
        started = time.monotonic()
        try:
            response = self._provider.complete(request)
        except ModelProviderError as error:
            self._write(
                Transcript(
                    provider=self._provider_id,
                    model_id=self._model_id,
                    prompt_version=PROMPT_VERSION,
                    parameters=self._parameters,
                    prompt_digest=prompt_hash,
                    schema_version=SCHEMA_VERSION,
                    request_digest=digest,
                    request=cast(dict[str, JsonValue], material),
                    error_code=error.code,
                    failure_stage=error.failure_stage,
                    failure_details=error.failure_details,
                    http_status=error.status_code,
                    attempts=error.attempts,
                    tokens_used=error.tokens_used,
                    latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                    recorded_at=datetime.now(UTC),
                )
            )
            raise
        safe_response = _redact(cast(JsonValue, response.output))
        self._write(
            Transcript(
                provider=self._provider_id,
                model_id=self._model_id,
                parameters=self._parameters,
                prompt_version=PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                prompt_digest=prompt_hash,
                request_digest=digest,
                request=cast(dict[str, JsonValue], material),
                response=cast(dict[str, JsonValue], safe_response),
                usage=cast(dict[str, JsonValue], _redact(response.usage.model_dump(mode="json"))),
                response_digest=hashlib.sha256(
                    _canonical(
                        {
                            "output": safe_response,
                            "usage": response.usage.model_dump(mode="json"),
                        }
                    )
                ).hexdigest(),
                attempts=response.usage.attempts,
                tokens_used=response.usage.total_tokens,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                recorded_at=datetime.now(UTC),
            )
        )
        return response

    def _write(self, transcript: Transcript) -> None:
        path = self._directory / f"call-{self._sequence:04d}.json"
        self._sequence += 1
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            transcript.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)


class ReplayProvider:
    """Replay recorded calls, refusing changed requests or corrupt transcript files."""

    provider_id = "replay"
    model_id = "recorded"

    def __init__(
        self,
        directory: Path,
        *,
        model_id: str = "fake-v1",
        parameters: dict[str, JsonValue] | None = None,
    ) -> None:
        self._paths = tuple(sorted(directory.glob("call-*.json")))
        self._index = 0
        self._model_id = model_id
        self._parameters = parameters or {}

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self._index >= len(self._paths):
            raise ModelProviderError("script_exhausted", "transcript is exhausted")
        path = self._paths[self._index]
        try:
            transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            raise ModelProviderError("invalid_response", "transcript is invalid") from None
        digest, prompt_hash = request_signature(
            request, model_id=self._model_id, parameters=self._parameters
        )
        expected_request = _redact(cast(JsonValue, request_material(request)))
        if (
            transcript.request_digest != digest
            or transcript.prompt_digest != prompt_hash
            or transcript.request != expected_request
            or transcript.model_id != self._model_id
            or transcript.parameters != self._parameters
            or transcript.prompt_version != PROMPT_VERSION
            or transcript.schema_version != SCHEMA_VERSION
        ):
            raise ModelProviderError("invalid_response", "transcript request does not match")
        self._index += 1
        if transcript.error_code is not None:
            raise ModelProviderError(
                transcript.error_code,
                "recorded provider failure",
                attempts=max(1, transcript.attempts),
                tokens_used=transcript.tokens_used,
            )
        if transcript.response is None or transcript.usage is None:
            raise ModelProviderError("invalid_response", "transcript response is incomplete")
        expected_response_digest = hashlib.sha256(
            _canonical({"output": transcript.response, "usage": transcript.usage})
        ).hexdigest()
        if transcript.response_digest != expected_response_digest:
            raise ModelProviderError("invalid_response", "transcript response digest is invalid")
        try:
            return ModelResponse.model_validate(
                {"output": transcript.response, "usage": transcript.usage}
            )
        except ValidationError:
            raise ModelProviderError("invalid_response", "transcript response is invalid") from None
