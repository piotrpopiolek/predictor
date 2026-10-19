"""Recommendation gates, operator actions, and a telegram notice without a token."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.constants import PAYOUT_KEEP
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
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.domain import Period
from predictor.services.advisor.recommend import (
    RecommendationSetup,
    inactive_reason,
    promotion_block,
    withdraw_due,
)
from predictor.services.advisor.recommend_ops import (
    ActionError,
    accept_coupon,
    expire_due,
    modify_coupon,
    place_coupon,
    place_proposed,
    withdraw_promoted,
)
from predictor.services.advisor.sandbox import LegSnapshot, SandboxSlip
from predictor.services.advisor.telegram import deliver_pending, recommendation_text
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


@pytest.mark.asyncio
async def test_confirm_expires_when_the_score_changes() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _proposed(session)
        async with factory() as session, session.begin():
            await accept_coupon(session, coupon_id, NOW)
        async with factory() as session, session.begin():
            fixture = await session.get(Fixture, 881_104)
            assert fixture is not None
            fixture.goals_home = 1
        error: ActionError | None = None
        async with factory() as session, session.begin():
            try:
                await place_coupon(session, coupon_id, NOW)
            except ActionError as exc:
                error = exc
        assert error is not None and error.code == "expired"
        async with factory() as session:
            coupon = await session.get(AdvisorCoupon, coupon_id)
        assert coupon is not None and coupon.status == "expired"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_place_after_ttl_expires_instead_of_placing() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _proposed(session)
        async with factory() as session, session.begin():
            await accept_coupon(session, coupon_id, NOW)
        late = NOW + timedelta(seconds=181)
        async with factory() as session, session.begin():
            with pytest.raises(ActionError, match="expired"):
                await place_coupon(session, coupon_id, late, ttl_seconds=180)
            coupon = await session.get(AdvisorCoupon, coupon_id)
            leg = await session.scalar(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
            )
        assert coupon is not None and coupon.status == "expired"
        assert coupon.placed_at is None
        assert leg is not None and leg.status == "removed"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_expire_and_withdraw_skip_a_placed_coupon() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _proposed(session)
            version = await session.scalar(select(AdvisorStrategyVersion))
            assert version is not None
            session.add(
                AdvisorCoupon(
                    mode="sandbox",
                    strategy_version_id=version.id,
                    status="lost",
                    stake=Decimal("100.00"),
                    combined_odd=Decimal("2.000"),
                    payout_keep=PAYOUT_KEEP,
                    placed_at=NOW,
                    created_at=NOW,
                )
            )
        async with factory() as session, session.begin():
            await accept_coupon(session, coupon_id, NOW)
        async with factory() as session, session.begin():
            await place_coupon(session, coupon_id, NOW, ttl_seconds=180)
        late = NOW + timedelta(hours=1)
        async with factory() as session, session.begin():
            expired = await expire_due(session, late, ttl_seconds=180)
            note = await withdraw_promoted(session, Decimal("0"))
            coupon = await session.get(AdvisorCoupon, coupon_id)
            leg = await session.scalar(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
            )
            strategy = await session.scalar(
                select(AdvisorStrategy).where(AdvisorStrategy.name == "reco-llm")
            )
        assert expired == 0
        assert note is not None
        assert coupon is not None and coupon.status == "placed"
        assert leg is not None and leg.status == "open"
        assert strategy is not None and strategy.status == "sandbox"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_odd_correction_leaves_the_coupon_untouched() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _proposed(session)
        async with factory() as session, session.begin():
            try:
                await modify_coupon(
                    session,
                    coupon_id,
                    NOW,
                    stake=Decimal("20.00"),
                    odds={881_104: Decimal("0.900")},
                    remove=set(),
                )
            except ActionError as exc:
                assert exc.code == "odd"
                assert exc.commit is False
        async with factory() as session:
            coupon = await session.get(AdvisorCoupon, coupon_id)
            leg = await session.scalar(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
            )
        assert coupon is not None and coupon.status == "proposed"
        assert leg is not None and leg.odd == Decimal("1.700")
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_modify_stores_the_operator_odd_and_a_notice() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _proposed(session)
        async with factory() as session, session.begin():
            below = await modify_coupon(
                session,
                coupon_id,
                NOW,
                stake=Decimal("20.00"),
                odds={881_104: Decimal("1.400")},
                remove=set(),
            )
        assert below is True
        async with factory() as session:
            coupon = await session.get(AdvisorCoupon, coupon_id)
            leg = await session.scalar(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
            )
            notice = await session.scalar(select(AdvisorTelegramOutbox))
        assert coupon is not None and coupon.status == "modified"
        assert leg is not None and leg.odd == Decimal("1.400")
        assert notice is not None
        assert "bot" not in str(notice.payload)
        async with factory() as session, session.begin():
            sent = await deliver_pending(
                session,
                token="secret-token",
                chat_id="1",
                post=_post,
            )
        assert sent == 1
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


async def _post(token: str, chat_id: str, text: str) -> None:
    assert token == "secret-token"
    assert "bot" not in text
    assert chat_id == "1"


async def _proposed(session) -> int:
    session.add(League(id=881_101, name="advisor-reco"))
    session.add(Team(id=881_102, name="Home"))
    session.add(Team(id=881_103, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=881_104,
            league_id=881_101,
            season=2026,
            home_team_id=881_102,
            away_team_id=881_103,
            status_short="2H",
            goals_home=0,
            goals_away=1,
            date=NOW - timedelta(hours=1),
        )
    )
    await session.flush()
    strategy = AdvisorStrategy(name="reco-llm", kind="llm", status="promoted")
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
    ids = await place_proposed(
        session,
        strategy_id=strategy.id,
        strategy_version_id=version.id,
        slips=(
            SandboxSlip(
                (
                    LegSnapshot(
                        fixture_id=881_104,
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
        created_at=NOW,
    )
    assert ids
    return ids[0]


async def _wipe(session) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name == "reco-llm")
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    await session.execute(
        delete(AdvisorTelegramOutbox).where(
            AdvisorTelegramOutbox.kind == "recommendation"
        )
    )
    await session.execute(
        delete(AdvisorCouponEvent).where(AdvisorCouponEvent.coupon_id.in_(coupon_ids))
    )
    await session.execute(
        delete(AdvisorStrategyEvent).where(
            AdvisorStrategyEvent.strategy_id.in_(strategy_ids)
        )
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
    await session.execute(delete(Fixture).where(Fixture.id == 881_104))
    await session.execute(delete(Team).where(Team.id.in_((881_102, 881_103))))
    await session.execute(delete(League).where(League.id == 881_101))
