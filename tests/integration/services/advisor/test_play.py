"""A sandbox-llm coupon can be marked played without changing the virtual book."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import String, cast, delete, select

from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorLeg,
    AdvisorPlayClaim,
    AdvisorStrategy,
    AdvisorStrategyVersion,
    AdvisorTelegramOutbox,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.play import mark_not_played, mark_played
from predictor.services.advisor.sandbox import (
    LegSnapshot,
    SandboxSlip,
    place_sandbox_coupons,
)

NOW = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)
NAME = "play-claim-llm"


def _leg(fixture_id: int) -> SandboxSlip:
    return SandboxSlip(
        (
            LegSnapshot(
                fixture_id=fixture_id,
                period="regulation",
                goals_home=0,
                goals_away=1,
                odd=Decimal("1.700"),
                n=1,
            ),
        )
    )


@pytest.mark.asyncio
async def test_played_coupon_keeps_the_sandbox_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADVISOR_RECOMMENDATION_STAKE", "20")
    monkeypatch.setenv("ADVISOR_RECOMMENDATION_LOSS_LIMIT", "1000000")
    monkeypatch.setenv("ADVISOR_OPERATOR_HOURS", "0-24")
    monkeypatch.setenv("ADVISOR_RECOMMENDATION_COUPON_LIMIT", "100000")
    monkeypatch.setenv("ADVISOR_WITHDRAW_YIELD", "0")
    monkeypatch.setattr("predictor.services.advisor.play.SANDBOX_LLM_NAME", NAME)
    monkeypatch.setattr("predictor.services.advisor.sandbox.SANDBOX_LLM_NAME", NAME)
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    version_id = 0
    strategy_id = 0
    try:
        async with factory() as session, session.begin():
            version_id, strategy_id = await _seed(session)
        async with factory() as session, session.begin():
            placed = await place_sandbox_coupons(
                session,
                strategy_id=strategy_id,
                strategy_version_id=version_id,
                slips=(_leg(883_004), _leg(883_014), _leg(9_883_024)),
                cycle_id=None,
                placed_at=NOW,
                kind="llm",
            )
        assert len(placed.coupon_ids) == 3
        first, second, third = placed.coupon_ids
        async with factory() as session:
            claim = await session.scalar(
                select(AdvisorPlayClaim).where(
                    AdvisorPlayClaim.sandbox_coupon_id == first
                )
            )
            text = await session.scalar(
                select(AdvisorTelegramOutbox.payload).where(
                    AdvisorTelegramOutbox.kind == "play_claim",
                    cast(AdvisorTelegramOutbox.payload, String).like(f"%/{first}%"),
                )
            )
        assert claim is not None and claim.status == "pending"
        assert text is not None and f"/advisor/sandbox/{first}" in str(text)
        async with factory() as session, session.begin():
            code, account_id, below = await mark_played(
                session,
                first,
                combined=Decimal("1.800"),
                stake=Decimal("12.50"),
                now=NOW,
            )
        assert code == "played" and below is False and account_id is not None
        async with factory() as session, session.begin():
            again, again_id, _below = await mark_played(
                session,
                first,
                combined=Decimal("1.900"),
                stake=Decimal("10.00"),
                now=NOW,
            )
        assert again == "played" and again_id == account_id
        async with factory() as session:
            sandbox = await session.get(AdvisorCoupon, first)
            account = await session.get(AdvisorCoupon, account_id)
            accounts = (
                await session.scalars(
                    select(AdvisorCoupon.id).where(
                        AdvisorCoupon.strategy_version_id == version_id,
                        AdvisorCoupon.mode == "recommendation",
                    )
                )
            ).all()
        assert sandbox is not None and sandbox.combined_odd == Decimal("1.700")
        assert sandbox.status == "open"
        assert account is not None and account.status == "placed"
        assert account.combined_odd == Decimal("1.800")
        assert account.stake == Decimal("12.50")
        assert accounts == [account_id]
        async with factory() as session, session.begin():
            for fixture_id, goals_home in ((883_014, 2), (9_883_024, 3)):
                match = await session.get(Fixture, fixture_id)
                assert match is not None
                match.goals_home = goals_home
        async with factory() as session, session.begin():
            later, later_id, later_below = await mark_played(
                session,
                second,
                combined=Decimal("1.800"),
                stake=Decimal("12.50"),
                now=NOW,
            )
        assert later == "played" and later_below is False and later_id is not None
        async with factory() as session:
            later_leg = await session.scalar(
                select(AdvisorLeg).where(
                    AdvisorLeg.coupon_id == later_id,
                    AdvisorLeg.mode == "recommendation",
                )
            )
        assert later_leg is not None
        assert later_leg.goals_home == 0
        assert later_leg.goals_away == 1
        assert later_leg.period == "regulation"
        assert later_leg.n == 1
        async with factory() as session, session.begin():
            missed = await mark_not_played(session, third, NOW)
        assert missed == "not_played"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, strategy_id, version_id)
        await engine.dispose()


async def _seed(session) -> tuple[int, int]:
    session.add(League(id=883_001, name=NAME))
    session.add(Team(id=883_002, name="Home"))
    session.add(Team(id=883_003, name="Away"))
    session.add(Team(id=883_012, name="Home 2"))
    session.add(Team(id=883_013, name="Away 2"))
    await session.flush()
    session.add(
        Fixture(
            id=883_004,
            league_id=883_001,
            season=2026,
            home_team_id=883_002,
            away_team_id=883_003,
            status_short="2H",
            goals_home=0,
            goals_away=1,
        )
    )
    session.add(
        Fixture(
            id=883_014,
            league_id=883_001,
            season=2026,
            home_team_id=883_012,
            away_team_id=883_013,
            status_short="2H",
            goals_home=0,
            goals_away=1,
        )
    )
    session.add(Team(id=9_883_022, name="Home 3"))
    session.add(Team(id=9_883_023, name="Away 3"))
    await session.flush()
    session.add(
        Fixture(
            id=9_883_024,
            league_id=883_001,
            season=2026,
            home_team_id=9_883_022,
            away_team_id=9_883_023,
            status_short="2H",
            goals_home=0,
            goals_away=1,
        )
    )
    strategy = AdvisorStrategy(name=NAME, kind="llm", status="promoted")
    session.add(strategy)
    await session.flush()
    version = AdvisorStrategyVersion(
        strategy_id=strategy.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=83,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    session.add(version)
    await session.flush()
    return version.id, strategy.id


async def _wipe(session, strategy_id: int, version_id: int) -> None:
    if version_id:
        coupon_ids = select(AdvisorCoupon.id).where(
            AdvisorCoupon.strategy_version_id == version_id
        )
        await session.execute(
            delete(AdvisorPlayClaim).where(
                AdvisorPlayClaim.sandbox_coupon_id.in_(coupon_ids)
            )
        )
        await session.execute(
            delete(AdvisorCouponEvent).where(
                AdvisorCouponEvent.coupon_id.in_(coupon_ids)
            )
        )
        await session.execute(
            delete(AdvisorTelegramOutbox).where(
                cast(AdvisorTelegramOutbox.payload, String).like("%883_%")
            )
        )
    if strategy_id:
        await session.execute(
            delete(AdvisorLeg).where(AdvisorLeg.strategy_id == strategy_id)
        )
    if version_id:
        await session.execute(
            delete(AdvisorCoupon).where(AdvisorCoupon.strategy_version_id == version_id)
        )
        await session.execute(
            delete(AdvisorStrategyVersion).where(
                AdvisorStrategyVersion.id == version_id
            )
        )
    if strategy_id:
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == strategy_id)
        )
    await session.execute(
        delete(Fixture).where(Fixture.id.in_((883_004, 883_014, 9_883_024)))
    )
    await session.execute(
        delete(Team).where(
            Team.id.in_((883_002, 883_003, 883_012, 883_013, 9_883_022, 9_883_023))
        )
    )
    await session.execute(delete(League).where(League.id == 883_001))
