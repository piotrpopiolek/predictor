"""Retry-After parsing and tenacity wait that prefers the header over jitter."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tenacity import RetryCallState
from tenacity.wait import wait_base, wait_exponential_jitter

from predictor.client.errors import RetryableHttpError


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return max(0.0, float(stripped))
    except ValueError:
        parsed = parsedate_to_datetime(stripped)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


class WaitRetryAfterOrJitter(wait_base):
    def __init__(self) -> None:
        self._fallback = wait_exponential_jitter(initial=1.0, max=60.0, jitter=1.0)

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, RetryableHttpError) and exc.retry_after is not None:
                return float(exc.retry_after)
        return float(self._fallback(retry_state))
