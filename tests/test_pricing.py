from decimal import Decimal

from tokenflow_cli.pricing import Pricing


def test_pricing_uses_minor_units_per_million() -> None:
    pricing = Pricing(
        currency="USD",
        input_per_million_cents=300,
        output_per_million_cents=1500,
    )
    cost = pricing.total_microcents(1_000_000, 1_000_000)
    assert pricing.format_spend(cost) == "USD 18.000000"


def test_cny_pricing_supports_decimal_multiplier() -> None:
    pricing = Pricing(
        currency="CNY",
        input_per_million_cents=Decimal("75"),
        output_per_million_cents=Decimal("450"),
        multiplier=Decimal("1.25"),
    )
    cost = pricing.total_microcents(1_000_000, 1_000_000)
    assert pricing.format_spend(cost) == "CNY 6.562500"
