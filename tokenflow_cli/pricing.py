from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class Pricing:
    """Prices are minor currency units per million tokens."""

    currency: str = "USD"
    input_per_million_cents: Decimal = Decimal("0")
    output_per_million_cents: Decimal = Decimal("0")
    multiplier: Decimal = Decimal("1")

    def input_microcents(self, tokens: int) -> int:
        return self._scaled_microcents(tokens, self.input_per_million_cents)

    def output_microcents(self, tokens: int) -> int:
        return self._scaled_microcents(tokens, self.output_per_million_cents)

    def total_microcents(self, prompt_tokens: int, completion_tokens: int) -> int:
        return self.input_microcents(prompt_tokens) + self.output_microcents(
            completion_tokens
        )

    def format_spend(self, microcents: int) -> str:
        return f"{self.currency} {microcents / 100_000_000:.6f}"

    def _scaled_microcents(self, tokens: int, price_cents: Decimal) -> int:
        value = Decimal(max(0, tokens)) * price_cents * self.multiplier
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
