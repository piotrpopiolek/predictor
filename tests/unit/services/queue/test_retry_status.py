"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from predictor.services.queue import (
    empty_retry_status,
    error_retry_status,
)


def test_error_retry_grows_then_stops_after_ten() -> None:
    now = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    params: dict[str, object] = {"player": 0}
    delays: list[float] = []
    for _ in range(10):
        status, params = error_retry_status(params, now=now)
        assert status == "retryable_error"
        due = datetime.fromisoformat(str(params["next_attempt_at"]))
        delays.append((due - now).total_seconds())
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600]
    status, params = error_retry_status(params, now=now)
    assert status == "permanent_error"
    assert params["error_retries"] == 11
    assert "next_attempt_at" not in params


def test_empty_retry_backs_off_then_stops() -> None:
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    status, params = empty_retry_status({}, now=now, max_attempts=3, backoff_seconds=60)
    assert status == "retryable_error"
    assert params["empty_attempts"] == 1
    assert params["next_attempt_at"] == (now + timedelta(seconds=60)).isoformat()
    status, params = empty_retry_status(
        params, now=now, max_attempts=3, backoff_seconds=60
    )
    assert status == "retryable_error"
    status, params = empty_retry_status(
        params, now=now, max_attempts=3, backoff_seconds=60
    )
    assert status == "coverage_empty"
    assert "next_attempt_at" not in params
