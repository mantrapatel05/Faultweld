"""Token cost calculation from an explicitly configured price table."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def token_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_price_per_million_usd: Decimal,
    output_price_per_million_usd: Decimal,
) -> Decimal:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must not be negative")
    raw = (
        Decimal(input_tokens) * input_price_per_million_usd
        + Decimal(output_tokens) * output_price_per_million_usd
    ) / Decimal(1_000_000)
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
