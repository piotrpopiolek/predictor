from __future__ import annotations

from datetime import UTC, datetime, timedelta

from predictor.services.health import live_poll_gauges
from predictor.services.metrics import render_metrics


def test_live_poll_gauges_ignores_future_captured_at() -> None:
    now = datetime(2026, 9, 13, 14, 55, tzinfo=UTC)
    future = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    assert live_poll_gauges(future, now) == (0.0, 0.0)


def test_live_poll_gauges_age_from_past_snapshot() -> None:
    now = datetime(2026, 9, 13, 14, 55, tzinfo=UTC)
    captured = now - timedelta(seconds=42)
    unix, age = live_poll_gauges(captured, now)
    assert unix == captured.timestamp()
    assert age == 42.0


def test_render_metrics_includes_live_age_gauge() -> None:
    body = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={},
        live_snapshot_age_seconds=12.5,
    )
    assert "predictor_live_snapshot_age_seconds" in body
    assert "12.5" in body


def test_render_metrics_includes_quota_used() -> None:
    body = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={},
        quota_remaining=1200,
        quota_used=6300,
    )
    assert "predictor_quota_used" in body
    assert "6300" in body
    assert "predictor_quota_remaining" in body
    assert "1200" in body


def test_render_metrics_includes_live_poll_interval() -> None:
    body = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={},
        live_poll_interval_seconds=467.676,
    )
    assert "predictor_live_poll_interval_seconds" in body
    assert "467.676" in body


def test_render_metrics_includes_quota_seconds_until_reset() -> None:
    body = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={},
        quota_seconds_until_reset=14340,
    )
    assert "predictor_quota_seconds_until_reset" in body
    assert "14340" in body
