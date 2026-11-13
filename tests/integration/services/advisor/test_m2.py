"""Shared board candidates and settlement after the match is final."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from predictor.constants import PAYOUT_KEEP
from predictor.models.advisor import (
    AdvisorCounterfactual,
    AdvisorCoupon,
    AdvisorDecision,
    AdvisorLeg,
    AdvisorSettlementEvent,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.settle import settle_finished
from predictor.services.advisor.tick import board_snapshots
from predictor.services.live_board.domain import LiveMatch, PrematchOdds

NOW = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)


def _live(**overrides: object) -> LiveMatch:
    base = dict(
        fixture_id=1,
        country="c",
        league="l",
        round=None,
        home="H",
        away="A",
        home_logo=None,
        away_logo=None,
        goals_home=1,
        goals_away=1,
        status_short="2H",
        status_long=None,
        elapsed=60,
        extra=None,
        home_team_id=10,
        away_team_id=20,
        prediction_winner_team_id=10,
        prematch=PrematchOdds("1.50", "4.0", "6.0", "Match Winner", "Bet365"),
    )
    base.update(overrides)
    return LiveMatch(**base)


def test_strategies_share_the_board_list() -> None:
    drawing = _live(fixture_id=2)
    losing = _live(fixture_id=3, goals_home=0, goals_away=1, elapsed=70)
    two_down = _live(fixture_id=4, goals_home=0, goals_away=2, elapsed=70, away_reds=1)
    selected = board_snapshots((drawing, losing, two_down))
    assert [item.fixture_id for item in selected] == [3]
    assert [item.rule for item in selected] == ["favorite_losing"]
    assert selected[0].favorite_odd == Decimal("1.50")


@pytest.mark.asyncio
async def test_settlement_wins_once_and_records_a_counterfactual() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(2, 1), status="FT")
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                leg = await session.get(AdvisorLeg, ids["leg"])
                events = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorSettlementEvent)
                    .where(AdvisorSettlementEvent.coupon_id == ids["coupon"])
                )
                fact = await session.scalar(
                    select(AdvisorCounterfactual).where(
                        AdvisorCounterfactual.decision_id == ids["decision"]
                    )
                )
            assert coupon is not None and coupon.status == "won"
            assert coupon.combined_odd == Decimal("1.700")
            assert leg is not None and leg.status == "won"
            assert events == 2
            assert fact is not None and fact.goal_scored is True
            assert fact.odd == Decimal("1.700")
            async with session.begin():
                again = await settle_finished(session, NOW)
            assert again == 0
            async with session.begin():
                events_after = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorSettlementEvent)
                    .where(AdvisorSettlementEvent.coupon_id == ids["coupon"])
                )
            assert events_after == 2
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_coverage_empty_detail_refresh_still_settles() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(2, 1), status="FT")
                task = await session.scalar(
                    select(EtlTask).where(EtlTask.fixture_id == ids["fixture"])
                )
                assert task is not None
                task.status = "coverage_empty"
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
            assert coupon is not None and coupon.status == "won"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_unfinished_match_stays_open_until_the_detail_refresh() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(
                    session, fulltime=(2, 1), status="FT", refreshed=False
                )
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 0
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
            assert coupon is not None and coupon.status == "open"
            async with session.begin():
                facts = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorCounterfactual)
                    .where(AdvisorCounterfactual.decision_id == ids["decision"])
                )
            assert facts == 0
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_score_correction_rewrites_the_coupon() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(2, 1), status="FT")
            async with session.begin():
                await settle_finished(session, NOW)
            async with session.begin():
                fixture = await session.get(Fixture, ids["fixture"])
                assert fixture is not None
                fixture.goals_home_fulltime = 0
                fixture.goals_away_fulltime = 1
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                sources = (
                    await session.scalars(select(AdvisorSettlementEvent.source))
                ).all()
                fact = await session.scalar(
                    select(AdvisorCounterfactual).where(
                        AdvisorCounterfactual.decision_id == ids["decision"]
                    )
                )
            assert coupon is not None and coupon.status == "lost"
            assert "correction" in sources
            assert fact is not None and fact.goal_scored is False
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cancelled_match_voids_without_a_score() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(
                    session, fulltime=None, status="CANC", refreshed=False
                )
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                leg = await session.get(AdvisorLeg, ids["leg"])
            assert coupon is not None and coupon.status == "void"
            assert leg is not None and leg.status == "void"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_extra_time_goal_does_not_win_regulation() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(
                    session,
                    fulltime=(0, 0),
                    status="AET",
                    extratime=(1, 0),
                    n=0,
                )
            async with session.begin():
                await settle_finished(session, NOW)
            async with session.begin():
                leg = await session.get(AdvisorLeg, ids["leg"])
            assert leg is not None and leg.status == "lost"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_lost_leg_closes_the_coupon_while_the_other_match_is_live() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(0, 0), status="FT", n=0)
                live = await _open_leg(session, ids, fixture_id=880_805, status="2H")
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                live_leg = await session.get(AdvisorLeg, live)
            assert coupon is not None and coupon.status == "lost"
            assert live_leg is not None and live_leg.status == "open"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_accepted_recommendation_waits_for_placed() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(2, 1), status="FT")
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                assert coupon is not None
                coupon.mode = "recommendation"
                coupon.status = "accepted"
                leg = await session.get(AdvisorLeg, ids["leg"])
                assert leg is not None
                leg.mode = "recommendation"
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 0
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                leg = await session.get(AdvisorLeg, ids["leg"])
            assert coupon is not None and coupon.status == "accepted"
            assert leg is not None and leg.status == "open"
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                assert coupon is not None
                coupon.status = "placed"
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
            assert coupon is not None and coupon.status == "won"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_removed_leg_drops_out_and_the_rest_can_win() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(session, fulltime=(2, 1), status="FT")
                removed = await _open_leg(
                    session, ids, fixture_id=880_805, status="2H", leg_status="removed"
                )
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                assert coupon is not None
                coupon.combined_odd = Decimal("2.550")
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                dropped = await session.get(AdvisorLeg, removed)
            assert coupon is not None and coupon.status == "won"
            assert coupon.combined_odd == Decimal("1.700")
            assert dropped is not None and dropped.status == "removed"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_removed_leg_does_not_block_a_void_coupon() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                ids = await _seed(
                    session, fulltime=None, status="CANC", refreshed=False
                )
                removed = await _open_leg(
                    session, ids, fixture_id=880_805, status="2H", leg_status="removed"
                )
            async with session.begin():
                changed = await settle_finished(session, NOW)
            assert changed == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, ids["coupon"])
                dropped = await session.get(AdvisorLeg, removed)
            assert coupon is not None and coupon.status == "void"
            assert dropped is not None and dropped.status == "removed"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


async def _seed(
    session,
    *,
    fulltime: tuple[int, int] | None,
    status: str,
    refreshed: bool = True,
    extratime: tuple[int, int] | None = None,
    n: int = 1,
) -> dict[str, int]:
    session.add(League(id=880_801, name="advisor-settle"))
    session.add(Team(id=880_802, name="Home"))
    session.add(Team(id=880_803, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=880_804,
            league_id=880_801,
            season=2026,
            home_team_id=880_802,
            away_team_id=880_803,
            status_short=status,
            date=NOW - timedelta(hours=3),
            goals_home_fulltime=None if fulltime is None else fulltime[0],
            goals_away_fulltime=None if fulltime is None else fulltime[1],
            goals_home_extratime=None if extratime is None else extratime[0],
            goals_away_extratime=None if extratime is None else extratime[1],
        )
    )
    await session.flush()
    if refreshed:
        session.add(
            EtlTask(
                endpoint="/fixtures",
                fixture_id=880_804,
                params={"id": 880_804},
                status="complete",
            )
        )
    strategy = AdvisorStrategy(name="advisor-settle", kind="llm", status="sandbox")
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
    cycle_id = None
    decision = AdvisorDecision(
        strategy_id=strategy.id,
        fixture_id=880_804,
        period="regulation",
        goals_home=0,
        goals_away=1,
        ordinal=2,
        candidate_rule="favorite_losing",
        decision="reject",
        reasons=["late_minute"],
        odd_at_eval=Decimal("1.700"),
        n=n,
    )
    session.add(decision)
    await session.flush()
    coupon = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=version.id,
        status="open",
        stake=Decimal("10.00"),
        combined_odd=Decimal("1.700"),
        proposed_combined_odd=Decimal("1.700"),
        payout_keep=PAYOUT_KEEP,
        placed_at=NOW - timedelta(hours=2),
    )
    session.add(coupon)
    await session.flush()
    leg = AdvisorLeg(
        coupon_id=coupon.id,
        strategy_id=strategy.id,
        fixture_id=880_804,
        mode="sandbox",
        period="regulation",
        n=n,
        odd=Decimal("1.700"),
        proposed_odd=Decimal("1.700"),
        goals_home=0,
        goals_away=1,
        status="open",
    )
    session.add(leg)
    await session.flush()
    return {
        "coupon": coupon.id,
        "leg": leg.id,
        "decision": decision.id,
        "fixture": 880_804,
        "strategy": strategy.id,
        "version": version.id,
        "cycle": cycle_id or 0,
    }


async def _open_leg(
    session,
    ids: dict[str, int],
    *,
    fixture_id: int,
    status: str,
    leg_status: str = "open",
) -> int:
    session.add(
        Fixture(
            id=fixture_id,
            league_id=880_801,
            season=2026,
            home_team_id=880_802,
            away_team_id=880_803,
            status_short=status,
            date=NOW - timedelta(hours=1),
        )
    )
    await session.flush()
    leg = AdvisorLeg(
        coupon_id=ids["coupon"],
        strategy_id=ids["strategy"],
        fixture_id=fixture_id,
        mode="sandbox",
        period="regulation",
        n=0,
        odd=Decimal("1.500"),
        proposed_odd=Decimal("1.500"),
        goals_home=0,
        goals_away=0,
        status=leg_status,
    )
    session.add(leg)
    await session.flush()
    return leg.id


async def _wipe(session) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(
        AdvisorStrategy.name == "advisor-settle"
    )
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    decision_ids = select(AdvisorDecision.id).where(
        AdvisorDecision.strategy_id.in_(strategy_ids)
    )
    await session.execute(
        delete(AdvisorSettlementEvent).where(
            AdvisorSettlementEvent.coupon_id.in_(coupon_ids)
        )
    )
    await session.execute(
        delete(AdvisorCounterfactual).where(
            AdvisorCounterfactual.decision_id.in_(decision_ids)
        )
    )
    await session.execute(
        delete(AdvisorLeg).where(AdvisorLeg.strategy_id.in_(strategy_ids))
    )
    await session.execute(
        delete(AdvisorDecision).where(AdvisorDecision.id.in_(decision_ids))
    )
    await session.execute(delete(AdvisorCoupon).where(AdvisorCoupon.id.in_(coupon_ids)))
    await session.execute(
        delete(AdvisorStrategyVersion).where(AdvisorStrategyVersion.id.in_(version_ids))
    )
    await session.execute(
        delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(strategy_ids))
    )
    await session.execute(
        delete(EtlTask).where(EtlTask.fixture_id.in_((880_804, 880_805)))
    )
    await session.execute(delete(Fixture).where(Fixture.id.in_((880_804, 880_805))))
    await session.execute(delete(Team).where(Team.id.in_((880_802, 880_803))))
    await session.execute(delete(League).where(League.id == 880_801))
