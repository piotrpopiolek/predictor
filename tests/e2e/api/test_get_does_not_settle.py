"""App-surface case. The service tests stay in integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.services.ingest.test_architecture_behavior import (
    _BOARD,
    _BOARD_AWAY,
    _BOARD_HOME,
    _BOARD_LEAGUE,
    _STRATEGY,
    SessionFactory,
    _database,
    _wipe_live,
)

from predictor.api.main import create_app
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
    AdvisorTelegramOutbox,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.models.operator_bets import OperatorBet
from predictor.services.advisor.domain import Period
from predictor.services.advisor.recommend_ops import place_proposed
from predictor.services.advisor.sandbox import LegSnapshot, SandboxSlip


async def _wipe_board(session: AsyncSession) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name == _STRATEGY)
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    for coupon_id in list(await session.scalars(coupon_ids)):
        await session.execute(
            delete(AdvisorTelegramOutbox).where(
                cast(AdvisorTelegramOutbox.payload, String).like(
                    f"%/advisor/recommendations/{coupon_id}%"
                )
            )
        )
    await session.execute(
        delete(AdvisorCouponEvent).where(AdvisorCouponEvent.coupon_id.in_(coupon_ids))
    )
    await session.execute(
        delete(AdvisorLeg).where(AdvisorLeg.coupon_id.in_(coupon_ids))
    )
    await session.execute(delete(AdvisorCoupon).where(AdvisorCoupon.id.in_(coupon_ids)))
    await session.execute(
        delete(AdvisorStrategyEvent).where(
            AdvisorStrategyEvent.strategy_id.in_(strategy_ids)
        )
    )
    await session.execute(
        delete(AdvisorStrategyVersion).where(AdvisorStrategyVersion.id.in_(version_ids))
    )
    await session.execute(
        delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(strategy_ids))
    )
    await session.execute(delete(OperatorBet).where(OperatorBet.fixture_id == _BOARD))
    await _wipe_live(session, _BOARD, _BOARD_LEAGUE)
    await session.execute(delete(Team).where(Team.id.in_((_BOARD_HOME, _BOARD_AWAY))))


@pytest.mark.asyncio
async def test_get_routes_do_not_settle_or_expire() -> None:
    created = datetime.now(UTC) - timedelta(minutes=10)
    async with _database() as factory:
        try:
            async with factory() as session, session.begin():
                await _wipe_board(session)
                session.add(League(id=_BOARD_LEAGUE, name="arch-board"))
                session.add(Team(id=_BOARD_HOME, name="Home"))
                session.add(Team(id=_BOARD_AWAY, name="Away"))
                await session.flush()
                session.add(
                    Fixture(
                        id=_BOARD,
                        league_id=_BOARD_LEAGUE,
                        season=2026,
                        home_team_id=_BOARD_HOME,
                        away_team_id=_BOARD_AWAY,
                        status_short="FT",
                        goals_home=1,
                        goals_away=0,
                        goals_home_fulltime=1,
                        goals_away_fulltime=0,
                    )
                )
                await session.flush()
                session.add(
                    OperatorBet(
                        fixture_id=_BOARD,
                        odd=Decimal("1.800"),
                        stake=Decimal("21.00"),
                        status="open",
                        placed_at=created,
                        league="arch-board",
                        home_name="Home",
                        away_name="Away",
                        goals_home=0,
                        goals_away=0,
                        status_short="1H",
                        payout_keep=Decimal("0.880"),
                    )
                )
                strategy = AdvisorStrategy(name=_STRATEGY, kind="llm", status="sandbox")
                session.add(strategy)
                await session.flush()
                version = AdvisorStrategyVersion(
                    strategy_id=strategy.id,
                    version_no=1,
                    prompt_version="p1",
                    min_combined_odd=Decimal("1.600"),
                    max_legs=3,
                    max_minute_regulation=80,
                    max_minute_extra=110,
                    wait_minutes=10,
                    candidate_rules=["favorite_losing"],
                )
                session.add(version)
                await session.flush()
                coupon_ids = await place_proposed(
                    session,
                    strategy_id=strategy.id,
                    strategy_version_id=version.id,
                    slips=(
                        SandboxSlip(
                            (
                                LegSnapshot(
                                    fixture_id=_BOARD,
                                    period=Period.REGULATION.value,
                                    goals_home=0,
                                    goals_away=1,
                                    odd=Decimal("1.700"),
                                    n=1,
                                ),
                            )
                        ),
                    ),
                    cycle_id=None,
                    stake=Decimal("20.00"),
                    created_at=created,
                )
                coupon = await session.get(AdvisorCoupon, coupon_ids[0])
                assert coupon is not None
                coupon.status = "accepted"
                coupon_id = int(coupon.id)
            events_before = await _count(factory, AdvisorCouponEvent, coupon_id)
            with TestClient(create_app()) as client:
                for path in (
                    "/advisor/report",
                    "/advisor/report.json",
                    "/advisor/recommendations",
                    f"/advisor/recommendations/{coupon_id}",
                    "/advisor/browse",
                    "/advisor/browse/advisor_coupons",
                    "/advisor/coupons",
                    "/advisor/coupons.json",
                    "/advisor/strategies",
                    "/live",
                    "/live.json",
                    "/live/bets",
                    "/live/bets.json",
                    "/live/next-goal",
                ):
                    response = client.get(path)
                    assert response.status_code == 200, path
            async with factory() as session:
                coupon = await session.get(AdvisorCoupon, coupon_id)
                bet = await session.scalar(
                    select(OperatorBet).where(OperatorBet.fixture_id == _BOARD)
                )
                leg = await session.scalar(
                    select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
                )
            assert coupon is not None and coupon.status == "accepted"
            assert coupon.placed_at is None
            assert bet is not None and bet.status == "open"
            assert bet.settled_at is None
            assert leg is not None and leg.status == "open"
            assert await _count(factory, AdvisorCouponEvent, coupon_id) == events_before
        finally:
            async with factory() as session, session.begin():
                await _wipe_board(session)


async def _count(
    factory: SessionFactory, model: type[AdvisorCouponEvent], coupon_id: int
) -> int:
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.coupon_id == coupon_id)
        )
    return int(count or 0)
