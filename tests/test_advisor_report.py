"""Sandbox yield and the baseline comparison on shared single-leg matches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.constants import PAYOUT_KEEP
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.report import (
    CouponView,
    LegView,
    StrategyView,
    build_report,
    load_yield_report,
)
from predictor.ui.advisor import render_yield_report

NOW = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
KEEP = PAYOUT_KEEP


def _leg(fixture_id: int, status: str, odd: str, period: str = "regulation") -> LegView:
    return LegView(
        fixture_id=fixture_id,
        status=status,
        odd=Decimal(odd),
        period=period,
    )


def _coupon(
    coupon_id: int,
    strategy_id: int,
    status: str,
    odd: str | None,
    legs: tuple[LegView, ...],
    stake: str = "10.00",
) -> CouponView:
    return CouponView(
        coupon_id=coupon_id,
        strategy_id=strategy_id,
        status=status,
        stake=Decimal(stake),
        combined_odd=None if odd is None else Decimal(odd),
        payout_keep=KEEP,
        legs=legs,
    )


def test_portfolio_yield_uses_the_keep_and_skips_voids() -> None:
    baseline = StrategyView(1, "baseline", "baseline")
    llm = StrategyView(2, "sandbox-llm", "llm")
    coupons = (
        _coupon(1, 1, "won", "1.700", (_leg(10, "won", "1.700"),)),
        _coupon(2, 1, "lost", "1.500", (_leg(11, "lost", "1.500"),)),
        _coupon(3, 1, "void", "1.800", (_leg(12, "void", "1.800"),)),
        _coupon(4, 1, "open", "1.600", (_leg(13, "open", "1.600"),)),
        _coupon(5, 2, "won", "2.000", (_leg(10, "won", "2.000"),)),
        _coupon(
            6,
            2,
            "won",
            "3.000",
            (_leg(10, "won", "1.500"), _leg(21, "won", "2.000")),
        ),
    )
    report = build_report((baseline, llm), coupons, period="all")
    base = report.books[0]
    assert base.played == 2
    assert base.hits == 1
    assert base.hit_rate == Decimal("0.5000")
    assert base.avg_odd == Decimal("1.600")
    assert base.net_pnl == Decimal("4.96") - Decimal("10.00")
    assert base.yield_net == Decimal("-0.2520")
    assert base.balance == Decimal("34.96")
    model = report.books[1]
    assert model.played == 2
    assert model.avg_odd == Decimal("2.500")
    assert model.net_pnl == Decimal("24.00")
    assert model.yield_net == Decimal("1.2000")
    pair = report.pairs[0]
    assert pair.matches == 1
    assert pair.baseline_hits == 1
    assert pair.strategy_hits == 1
    assert pair.baseline_yield == Decimal("0.4960")
    assert pair.strategy_yield == Decimal("0.7600")


def test_a_removed_leg_still_counts_as_one_match() -> None:
    baseline = StrategyView(1, "baseline", "baseline")
    llm = StrategyView(2, "sandbox-llm", "llm")
    coupons = (
        _coupon(1, 1, "won", "1.700", (_leg(10, "won", "1.700"),)),
        _coupon(
            2,
            2,
            "lost",
            "1.400",
            (_leg(10, "lost", "1.400"), _leg(11, "removed", "1.900")),
        ),
    )
    pair = build_report((baseline, llm), coupons, period="all").pairs[0]
    assert pair.matches == 1
    assert pair.strategy_hits == 0
    assert pair.strategy_yield == Decimal("-1.0000")


def test_period_filter_keeps_one_settlement_period() -> None:
    baseline = StrategyView(1, "baseline", "baseline")
    coupons = (
        _coupon(1, 1, "won", "1.700", (_leg(10, "won", "1.700"),)),
        _coupon(
            2,
            1,
            "won",
            "1.800",
            (_leg(11, "won", "1.800", period="extra_time"),),
        ),
    )
    mixed = _coupon(
        3,
        1,
        "won",
        "2.500",
        (
            _leg(12, "won", "1.500"),
            _leg(13, "won", "1.800", period="extra_time"),
        ),
    )
    regulation = build_report((baseline,), (*coupons, mixed), period="regulation")
    extra = build_report((baseline,), (*coupons, mixed), period="extra_time")
    assert regulation.books[0].played == 1
    assert regulation.books[0].yield_net == Decimal("0.4960")
    assert regulation.books[0].balance == Decimal("54.96")
    assert extra.books[0].played == 1
    assert extra.books[0].balance == Decimal("55.84")


def test_report_page_shows_the_net_yield() -> None:
    baseline = StrategyView(1, "baseline", "baseline")
    coupons = (
        _coupon(1, 1, "won", "1.700", (_leg(10, "won", "1.700"),)),
        _coupon(2, 1, "lost", "1.500", (_leg(11, "lost", "1.500"),)),
    )
    report = build_report((baseline,), coupons, period="all")
    html = render_yield_report(report)
    assert "50,00%" in html
    assert "1,600" in html
    assert "Śr. kurs" in html
    assert "baseline" in html
    assert "Te same mecze" in html
    assert "Wykresy" in html
    assert 'class="charts"' in html
    assert "Saldo" in html
    assert "Skuteczność" in html
    assert len(report.charts) == 1
    assert len(report.charts[0].points) == 2
    assert report.charts[0].points[0]["saldo"] == "54.96"
    assert report.charts[0].points[-1]["saldo"] == "44.96"
    assert report.charts[0].points[-1]["hit_overall"] == "50.00"


@pytest.mark.asyncio
async def test_loaded_report_matches_the_settled_coupons() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _seed(session)
        async with factory() as session:
            report = await load_yield_report(session, period="all")
        base = next(line for line in report.books if line.name == "report-baseline")
        pair = next(
            item
            for item in report.pairs
            if item.baseline_name == "report-baseline"
            and item.strategy_name == "report-llm"
        )
        assert base.hits == 1
        assert base.played == 1
        assert base.yield_net == Decimal("0.4960")
        assert pair.matches == 1
        assert pair.strategy_yield == Decimal("0.7600")
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


async def _seed(session) -> None:
    session.add(League(id=880_901, name="advisor-report"))
    session.add(Team(id=880_902, name="Home"))
    session.add(Team(id=880_903, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=880_904,
            league_id=880_901,
            season=2026,
            home_team_id=880_902,
            away_team_id=880_903,
            status_short="FT",
            date=NOW - timedelta(hours=3),
        )
    )
    await session.flush()
    baseline = AdvisorStrategy(
        name="report-baseline", kind="baseline", status="sandbox"
    )
    llm = AdvisorStrategy(name="report-llm", kind="llm", status="sandbox")
    session.add(baseline)
    session.add(llm)
    await session.flush()
    base_version = AdvisorStrategyVersion(
        strategy_id=baseline.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing", "tie_deficit"],
    )
    llm_version = AdvisorStrategyVersion(
        strategy_id=llm.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing", "tie_deficit"],
    )
    session.add(base_version)
    session.add(llm_version)
    await session.flush()
    base_coupon = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=base_version.id,
        status="won",
        stake=Decimal("10.00"),
        combined_odd=Decimal("1.700"),
        proposed_combined_odd=Decimal("1.700"),
        payout_keep=KEEP,
        placed_at=NOW - timedelta(hours=2),
    )
    llm_coupon = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=llm_version.id,
        status="won",
        stake=Decimal("10.00"),
        combined_odd=Decimal("2.000"),
        proposed_combined_odd=Decimal("2.000"),
        payout_keep=KEEP,
        placed_at=NOW - timedelta(hours=2),
    )
    session.add(base_coupon)
    session.add(llm_coupon)
    await session.flush()
    session.add(
        AdvisorLeg(
            coupon_id=base_coupon.id,
            strategy_id=baseline.id,
            fixture_id=880_904,
            mode="sandbox",
            period="regulation",
            n=1,
            odd=Decimal("1.700"),
            proposed_odd=Decimal("1.700"),
            goals_home=0,
            goals_away=1,
            status="won",
        )
    )
    session.add(
        AdvisorLeg(
            coupon_id=llm_coupon.id,
            strategy_id=llm.id,
            fixture_id=880_904,
            mode="sandbox",
            period="regulation",
            n=1,
            odd=Decimal("2.000"),
            proposed_odd=Decimal("2.000"),
            goals_home=0,
            goals_away=1,
            status="won",
        )
    )


async def _wipe(session) -> None:
    names = ("report-baseline", "report-llm")
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name.in_(names))
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    await session.execute(
        delete(AdvisorLeg).where(AdvisorLeg.strategy_id.in_(strategy_ids))
    )
    await session.execute(delete(AdvisorCoupon).where(AdvisorCoupon.id.in_(coupon_ids)))
    await session.execute(
        delete(AdvisorStrategyVersion).where(AdvisorStrategyVersion.id.in_(version_ids))
    )
    await session.execute(
        delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(strategy_ids))
    )
    await session.execute(delete(Fixture).where(Fixture.id == 880_904))
    await session.execute(delete(Team).where(Team.id.in_((880_902, 880_903))))
    await session.execute(delete(League).where(League.id == 880_901))
