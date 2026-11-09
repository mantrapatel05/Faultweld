"""Validated, non-secret configuration for OpenAI-compatible model providers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, repr=False)
class LlmConfig:
    provider_name: str = "openrouter"
    model_id: str = "recursive/flint-v1.0:free"
    max_output_tokens: int = 1024
    temperature: float = 0.0
    timeout_seconds: float = 45.0
    max_retries: int = 1
    input_price_per_million_usd: Decimal = Decimal("0")
    output_price_per_million_usd: Decimal = Decimal("0")
    price_as_of: str = "2026-11-09"
    max_run_cost_usd: Decimal = Decimal("0.05")
    base_url: str = "https://openrouter.ai/api/v1"
    allowed_hosts: tuple[str, ...] = ("openrouter.ai",)

    def __post_init__(self) -> None:
        if not self.provider_name.strip() or not self.model_id.strip():
            raise ValueError("provider_name and model_id are required")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not 0 <= self.temperature <= 1:
            raise ValueError("temperature must be between 0 and 1")
        if self.input_price_per_million_usd < 0 or self.output_price_per_million_usd < 0:
            raise ValueError("token prices must not be negative")
        if self.max_run_cost_usd < 0:
            raise ValueError("max_run_cost_usd must not be negative")
        if not self.base_url.startswith("https://"):
            raise ValueError("base_url must be an HTTPS URL with a hostname")
        authority = self.base_url.removeprefix("https://").split("/", 1)[0]
        if not authority or "@" in authority or ":" in authority:
            raise ValueError("base_url must not contain credentials")
        hostname = authority.lower()
        hosts = {host.lower() for host in self.allowed_hosts}
        if hostname not in hosts:
            raise ValueError("base_url host is not allow-listed")

    def __repr__(self) -> str:
        return (
            f"LlmConfig(provider_name={self.provider_name!r}, "
            f"model_id={self.model_id!r}, max_output_tokens={self.max_output_tokens!r})"
        )
