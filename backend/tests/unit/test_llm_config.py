from __future__ import annotations

from unittest import TestCase

from app.agent.llm.config import LlmConfig


class LlmConfigTests(TestCase):
    def test_default_targets_openrouter_model_and_https_host(self) -> None:
        config = LlmConfig()
        self.assertEqual(config.provider_name, "openrouter")
        self.assertEqual(config.model_id, "recursive/flint-v1.0:free")
        self.assertEqual(config.base_url, "https://openrouter.ai/api/v1")

    def test_rejects_non_https_or_non_allowlisted_hosts(self) -> None:
        with self.assertRaises(ValueError):
            LlmConfig(base_url="http://openrouter.ai/api/v1")
        with self.assertRaises(ValueError):
            LlmConfig(base_url="https://evil.example/api/v1")
        with self.assertRaises(ValueError):
            LlmConfig(base_url="https://user@openrouter.ai/api/v1")

    def test_repr_contains_no_secret_field(self) -> None:
        self.assertNotIn("OPENROUTER_API_KEY", repr(LlmConfig()))
