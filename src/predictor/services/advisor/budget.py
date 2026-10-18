"""Daily LLM budget. Sandbox may spend 70 percent; the rest is for recommendations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

DAILY_BUDGET_PLN = Decimal("10")
SANDBOX_SHARE = Decimal("0.70")
RESERVED_OUTPUT_TOKENS = 2048
_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.0001")


def call_cost(
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int,
    *,
    price_input: Decimal,
    price_output: Decimal,
) -> Decimal:
    """Price thoughts with the output rate. Result is quantized to 0.0001 PLN."""
    raw = (
        Decimal(input_tokens) * price_input
        + Decimal(output_tokens + thoughts_tokens) * price_output
    ) / _MILLION
    return raw.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_tokens(prompt: str) -> tuple[int, int, int]:
    """Input from the prompt length, plus a fixed output reserve. Thoughts are 0."""
    return max(1, len(prompt) // 4), RESERVED_OUTPUT_TOKENS, 0


def estimate_call_cost(
    prompt: str, *, price_input: Decimal, price_output: Decimal
) -> Decimal:
    """Cost of ``estimate_tokens`` at the configured prices."""
    input_tokens, output_tokens, thoughts_tokens = estimate_tokens(prompt)
    return call_cost(
        input_tokens,
        output_tokens,
        thoughts_tokens,
        price_input=price_input,
        price_output=price_output,
    )


def budget_cap(mode: str, limit: Decimal = DAILY_BUDGET_PLN) -> Decimal:
    if mode == "sandbox":
        return (limit * SANDBOX_SHARE).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    if mode == "recommendation":
        return limit
    raise ValueError("mode must be sandbox or recommendation")


def budget_allows(
    spent: Decimal,
    estimated: Decimal,
    *,
    mode: str,
    limit: Decimal = DAILY_BUDGET_PLN,
) -> bool:
    return spent + estimated <= budget_cap(mode, limit)


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
