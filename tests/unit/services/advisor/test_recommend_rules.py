"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from predictor.services.advisor.recommend import (
    RecommendationSetup,
    inactive_reason,
    promotion_block,
    withdraw_due,
)
from predictor.services.advisor.telegram import recommendation_text
from predictor.ui.recommendations import render_recommendation_card

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _setup(**overrides: object) -> RecommendationSetup:
    base = dict(
        stake=Decimal("20"),
        loss_limit=Decimal("100"),
        hours=(8, 22),
        league_ids=frozenset({39}),
        coupon_limit=10,
        ttl_seconds=180,
        withdraw_yield=Decimal("0"),
    )
    base.update(overrides)
    return RecommendationSetup(**base)


def test_recommendation_stays_inactive_without_a_stake() -> None:
    setup = _setup(stake=None)
    assert (
        inactive_reason(setup, NOW, coupons_today=0, loss_today=Decimal("0")) == "stake"
    )


def test_promotion_needs_sample_and_a_better_yield_than_baseline() -> None:
    assert (
        promotion_block(
            settled=10,
            yield_net=Decimal("0.10"),
            baseline_yield=Decimal("0.01"),
            drawdown=Decimal("0.10"),
        )
        == "sample"
    )
    assert (
        promotion_block(
            settled=200,
            yield_net=Decimal("0.05"),
            baseline_yield=Decimal("0.08"),
            drawdown=Decimal("0.10"),
        )
        == "baseline"
    )
    assert (
        promotion_block(
            settled=200,
            yield_net=Decimal("0.20"),
            baseline_yield=Decimal("0.05"),
            drawdown=Decimal("0.10"),
        )
        is None
    )


def test_withdraw_when_the_recent_yield_is_under_the_floor() -> None:
    assert withdraw_due(Decimal("-0.01"), Decimal("0")) is True
    assert withdraw_due(None, Decimal("0")) is False


def test_telegram_text_has_the_link_and_no_secret() -> None:
    text = recommendation_text(
        coupon_id=7,
        lines=("mecz 1 Over 1+0.5 kurs 1.700",),
        stake="20.00",
        combined="1.700",
        expires="2026-10-19T12:03:00+00:00",
        link="/advisor/recommendations/7",
    )
    assert "/advisor/recommendations/7" in text
    assert "bot" not in text
    html = render_recommendation_card(
        coupon_id=7,
        status="proposed",
        stake="20.00",
        combined="1.700",
        floor="1.600",
        lines=("mecz 1",),
        below=False,
    )
    assert 'type="password"' in html
    assert 'value="secret"' not in html
