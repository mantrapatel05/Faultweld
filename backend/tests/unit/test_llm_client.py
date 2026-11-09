from __future__ import annotations

import json
import os
from email.message import Message
from io import BytesIO
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from app.agent.llm.client import HttpLlmClient
from app.agent.llm.config import LlmConfig
from app.agent.llm.errors import LlmClientError


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self._body

    @property
    def status(self) -> int:
        return 200


class FakeOpener:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.request: Request | None = None
        self.timeout: float | None = None

    def open(self, request: Request, *, timeout: float) -> FakeHttpResponse:
        self.request = request
        self.timeout = timeout
        return self.response


class OpenRouterClientTests(TestCase):
    def test_request_and_response_follow_openrouter_chat_completions_shape(self) -> None:
        response = FakeHttpResponse(
            json.dumps(
                {
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            ).encode()
        )
        opener = FakeOpener(response)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "canary-key"}):
            client = HttpLlmClient(LlmConfig())
            with patch("app.agent.llm.client.urllib.request.build_opener", return_value=opener):
                result = client.complete(
                    prompt="synthetic prompt",
                    schema={"type": "object"},
                    model="stealth/crouton-alpha",
                    max_output_tokens=40,
                    temperature=0.0,
                    timeout_seconds=5.0,
                )
        assert opener.request is not None
        self.assertEqual(opener.request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer canary-key")
        self.assertEqual(opener.request.get_header("User-agent"), "Faultweld-Lite/0.1")
        request_body = opener.request.data
        assert isinstance(request_body, bytes)
        body = json.loads(request_body)
        self.assertEqual(body["model"], "stealth/crouton-alpha")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(result.text, "{}")
        self.assertEqual((result.input_tokens, result.output_tokens), (3, 2))
        self.assertNotIn("canary-key", repr(client))

    def test_groq_uses_its_base_url_key_and_supported_schema_shape(self) -> None:
        response = FakeHttpResponse(
            json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
        )
        opener = FakeOpener(response)
        config = LlmConfig(
            provider_name="groq",
            model_id="openai/gpt-oss-120b",
            base_url="https://api.groq.com/openai/v1",
            allowed_hosts=("api.groq.com",),
        )
        with patch.dict(os.environ, {"GROQ_API_KEY": "groq-canary"}):
            client = HttpLlmClient(config, api_key_env="GROQ_API_KEY")
            with patch("app.agent.llm.client.urllib.request.build_opener", return_value=opener):
                client.complete(
                    prompt="synthetic prompt",
                    schema={"type": "object"},
                    model="openai/gpt-oss-120b",
                    max_output_tokens=40,
                    temperature=0.0,
                    timeout_seconds=5.0,
                )
        assert opener.request is not None
        self.assertEqual(
            opener.request.full_url,
            "https://api.groq.com/openai/v1/chat/completions",
        )
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer groq-canary")
        body_bytes = opener.request.data
        assert isinstance(body_bytes, bytes)
        body = json.loads(body_bytes)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertNotIn("provider", body)
        self.assertNotIn("groq-canary", repr(client))

    def test_plain_connectivity_request_omits_structured_output_options(self) -> None:
        opener = FakeOpener(FakeHttpResponse(b'{"choices":[{"message":{"content":"OK"}}]}'))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "canary-key"}):
            client = HttpLlmClient(LlmConfig())
            with patch("app.agent.llm.client.urllib.request.build_opener", return_value=opener):
                result = client.complete(
                    prompt="Reply OK.",
                    schema=None,
                    model="stealth/crouton-alpha",
                    max_output_tokens=8,
                    temperature=0.0,
                    timeout_seconds=5.0,
                )
        assert opener.request is not None
        body_bytes = opener.request.data
        assert isinstance(body_bytes, bytes)
        self.assertNotIn("response_format", json.loads(body_bytes))
        self.assertEqual(result.text, "OK")

    def test_client_fails_closed_without_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LlmClientError) as raised:
                HttpLlmClient(LlmConfig())
        self.assertEqual(raised.exception.code, "auth_failed")
        self.assertNotIn("canary-key", str(raised.exception))

    def test_http_status_is_exposed_without_server_body(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            429,
            "rate limited",
            Message(),
            BytesIO(b"sensitive server details"),
        )
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "canary-key"}):
            client = HttpLlmClient(LlmConfig())
            with patch("app.agent.llm.client.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(LlmClientError) as raised:
                    client.complete(
                        prompt="probe",
                        schema=None,
                        model="stealth/crouton-alpha",
                        max_output_tokens=8,
                        temperature=0.0,
                        timeout_seconds=5.0,
                    )
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertNotIn("sensitive server details", str(raised.exception))
