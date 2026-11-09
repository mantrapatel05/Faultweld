"""The only module in the agent package that performs network I/O."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from app.agent.llm.config import LlmConfig
from app.agent.llm.errors import LlmClientError

MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class ClientResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    status_code: int = 200


class LlmClient(Protocol):
    def complete(
        self,
        *,
        prompt: str,
        schema: dict[str, object] | None,
        model: str,
        max_output_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> ClientResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        return None


class HttpLlmClient:
    """OpenAI-compatible chat-completions client; reads its key at construction."""

    def __init__(self, config: LlmConfig, *, api_key_env: str = "OPENROUTER_API_KEY") -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise LlmClientError("auth_failed")
        self._api_key = api_key
        self._base_url = config.base_url.rstrip("/")
        self._host = urlparse(config.base_url).hostname
        self._allowed_hosts = {host.lower() for host in config.allowed_hosts}

    def __repr__(self) -> str:
        return "HttpLlmClient(api_key=[redacted])"

    def complete(
        self,
        *,
        prompt: str,
        schema: dict[str, object] | None,
        model: str,
        max_output_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> ClientResponse:
        if self._host is None or self._host.lower() not in self._allowed_hosts:
            raise LlmClientError("unavailable")
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if self._base_url.startswith("https://openrouter.ai/"):
            payload["provider"] = {"require_parameters": True}
        if schema == {"type": "json_object"}:
            payload["response_format"] = schema
        elif schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "faultweld_output",
                    "strict": True,
                    "schema": schema,
                },
            }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._api_key}",
                "user-agent": "Faultweld-Lite/0.1",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        status_code: int | None = None
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                status_code = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise LlmClientError("auth_failed", status_code=status) from None
            if status == 429:
                raise LlmClientError("rate_limited", status_code=status) from None
            if 500 <= status <= 599:
                raise LlmClientError("unavailable", status_code=status) from None
            raise LlmClientError("invalid_response", status_code=status) from None
        except TimeoutError:
            raise LlmClientError("timeout") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise LlmClientError("timeout") from None
            raise LlmClientError("unavailable") from None
        except OSError:
            raise LlmClientError("unavailable") from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise LlmClientError("invalid_response")
        try:
            decoded = json.loads(raw)
            choices = decoded["choices"]
            message = choices[0]["message"]
            content = message["content"]
            usage = decoded.get("usage", {})
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            if not isinstance(content, str) or not content:
                raise ValueError("missing assistant content")
            if input_tokens is not None and (
                isinstance(input_tokens, bool)
                or not isinstance(input_tokens, int)
                or input_tokens < 0
            ):
                raise ValueError("invalid input usage")
            if output_tokens is not None and (
                isinstance(output_tokens, bool)
                or not isinstance(output_tokens, int)
                or output_tokens < 0
            ):
                raise ValueError("invalid output usage")
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            raise LlmClientError("invalid_response", status_code=status_code) from None
        return ClientResponse(content, input_tokens, output_tokens, status_code or 200)
