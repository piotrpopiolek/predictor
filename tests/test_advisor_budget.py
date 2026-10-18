"""Daily budget math. No database and no model call."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predictor.services.advisor.budget import (
    RESERVED_OUTPUT_TOKENS,
    budget_allows,
    budget_cap,
    call_cost,
    estimate_call_cost,
    utc_day_bounds,
)

PRICE_IN = Decimal("0.50")
PRICE_OUT = Decimal("1.25")


def test_call_cost_prices_thoughts_as_output() -> None:
    cost = call_cost(
        1_000_000,
        400_000,
        100_000,
        price_input=PRICE_IN,
        price_output=PRICE_OUT,
    )
    assert cost == Decimal("1.1250")


def test_estimate_reserves_output_tokens() -> None:
    prompt = "x" * 4000
    estimate = estimate_call_cost(prompt, price_input=PRICE_IN, price_output=PRICE_OUT)
    assert estimate == call_cost(
        1000,
        RESERVED_OUTPUT_TOKENS,
        0,
        price_input=PRICE_IN,
        price_output=PRICE_OUT,
    )


def test_sandbox_stops_at_seventy_percent_and_recommendation_keeps_the_rest() -> None:
    assert budget_cap("sandbox") == Decimal("7.0000")
    assert budget_cap("recommendation") == Decimal("10")
    assert budget_allows(Decimal("6.9900"), Decimal("0.0100"), mode="sandbox")
    assert not budget_allows(Decimal("7.0000"), Decimal("0.0001"), mode="sandbox")
    assert budget_allows(Decimal("7.0000"), Decimal("2.0000"), mode="recommendation")
    assert not budget_allows(
        Decimal("9.9900"), Decimal("0.0200"), mode="recommendation"
    )
    with pytest.raises(ValueError):
        budget_cap("summary")


def test_utc_day_starts_at_midnight() -> None:
    now = datetime(2026, 10, 18, 23, 30, tzinfo=UTC)
    start, end = utc_day_bounds(now)
    assert start == datetime(2026, 10, 18, tzinfo=UTC)
    assert end == datetime(2026, 10, 19, tzinfo=UTC)
    with pytest.raises(ValueError):
        utc_day_bounds(datetime(2026, 10, 18, 23, 30))
