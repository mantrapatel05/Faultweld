from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from app.agent.llm.pricing import token_cost_usd


class LlmPricingTests(TestCase):
    def test_cost_is_rounded_to_six_decimal_places(self) -> None:
        cost = token_cost_usd(
            input_tokens=1500,
            output_tokens=500,
            input_price_per_million_usd=Decimal("2"),
            output_price_per_million_usd=Decimal("4"),
        )
        self.assertEqual(cost, Decimal("0.005000"))

    def test_negative_token_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            token_cost_usd(
                input_tokens=-1,
                output_tokens=0,
                input_price_per_million_usd=Decimal("1"),
                output_price_per_million_usd=Decimal("1"),
            )
