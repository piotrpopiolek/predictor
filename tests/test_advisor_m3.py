"""Recommendation gates, operator actions, and a strategy proposal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.constants import PAYOUT_KEEP
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.llm.protocol import LlmCompletion
from predictor.services.advisor.recommend import (
    RecommendationSetup,
    hour_allowed,
    inactive_reason,
    league_allowed,
    max_drawdown,
    promotion_block,
    withdraw_due,
)
from predictor.services.advisor.recommend_ops import (
    ActionError,
    accept_coupon,
    modify_coupon,
    place_coupon,
    promote_strategy,
)
from predictor.services.advisor.summary import SummaryJob, parse_proposal, store_summary
from predictor.services.advisor.telegram import recommendation_text
from predictor.ui.advisor import render_recommendation_cards

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_PROPOSAL = (
    '{"min_combined_odd":"1.700","max_legs":2,'
    '"max_minute_regulation":80,"max_minute_extra":110,'
    '"wait_minutes":10,"notes":"węższy próg"}'
)


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


def test_recommendation_stays_inactive_without_operator_settings() -> None:
    bare = _setup(stake=None, hours=None, league_ids=frozenset())
    assert (
        inactive_reason(bare, NOW, coupons_today=0, loss_today=Decimal("0")) == "stake"
    )
    ready = _setup()
    assert inactive_reason(ready, NOW, coupons_today=0, loss_today=Decimal("0")) is None
    open_leagues = _setup(league_ids=frozenset())
    assert (
        inactive_reason(open_leagues, NOW, coupons_today=0, loss_today=Decimal("0"))
        is None
    )
    assert league_allowed(39, open_leagues, untracked=frozenset({874})) is True
    assert league_allowed(874, open_leagues, untracked=frozenset({874})) is False
    assert league_allowed(39, ready, untracked=frozenset()) is True
    assert league_allowed(140, ready, untracked=frozenset()) is False
    assert inactive_reason(ready, NOW, coupons_today=10, loss_today=Decimal("0")) == (
        "coupon_limit"
    )
    assert inactive_reason(ready, NOW, coupons_today=0, loss_today=Decimal("100")) == (
        "loss_limit_hit"
    )
    assert hour_allowed(23, (22, 6)) is True
    assert hour_allowed(12, (22, 6)) is False


def test_promotion_and_withdraw_use_the_sandbox_book() -> None:
    assert (
        promotion_block(
            settled=199,
            yield_net=Decimal("0.1"),
            baseline_yield=Decimal("0"),
            drawdown=Decimal("0.1"),
        )
        == "sample"
    )
    assert (
        promotion_block(
            settled=200,
            yield_net=Decimal("0.2"),
            baseline_yield=Decimal("0.1"),
            drawdown=Decimal("0.2"),
        )
        is None
    )
    assert withdraw_due(Decimal("-0.01"), Decimal("0")) is True
    assert withdraw_due(None, Decimal("0")) is False
    assert max_drawdown([Decimal("-250")]) == Decimal("0.2500")


def test_telegram_text_has_no_token() -> None:
    text = recommendation_text(
        coupon_id=4,
        lines=("A – B, 70', 0:1, Over 1.5 @ 1.70",),
        stake="20",
        combined="1.70",
        expires="2026-10-19T12:03:00+00:00",
        link="http://127.0.0.1:8000/advisor/recommendations/4",
    )
    assert "bot" not in text.casefold()
    assert "1.70" in text
    card = render_recommendation_cards(
        [
            {
                "id": 1,
                "status": "proposed",
                "stake": "20.00",
                "combined": "1.700",
                "remaining": 90,
                "below": True,
                "legs": [
                    {
                        "fixture_id": 9,
                        "score": "0:1",
                        "line": "1.5",
                        "odd": "1.700",
                        "proposed": "1.700",
                    }
                ],
            }
        ]
    )
    assert "poniżej progu" in card
    assert 'value="secret"' not in card


def test_summary_proposal_rejects_extra_fields() -> None:
    proposal = parse_proposal(_PROPOSAL)
    assert proposal.max_legs == 2


@pytest.mark.asyncio
async def test_summary_names_include_the_utc_day() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _seed(session)
            coupon = await session.get(AdvisorCoupon, coupon_id)
            assert coupon is not None
            version = await session.get(
                AdvisorStrategyVersion, coupon.strategy_version_id
            )
            assert version is not None
            job = SummaryJob(version.strategy_id, version.id, "prompt")
            first = await store_summary(
                session,
                job,
                LlmCompletion(
                    text=_PROPOSAL,
                    provider="google",
                    model="gemini-2.5-flash",
                    model_version="gemini-2.5-flash",
                    input_tokens=10,
                    output_tokens=10,
                    thoughts_tokens=0,
                ),
                cost=Decimal("0.0100"),
                now=NOW,
            )
            second = await store_summary(
                session,
                job,
                LlmCompletion(
                    text=_PROPOSAL,
                    provider="google",
                    model="gemini-2.5-flash",
                    model_version="gemini-2.5-flash",
                    input_tokens=10,
                    output_tokens=10,
                    thoughts_tokens=0,
                ),
                cost=Decimal("0.0100"),
                now=NOW + timedelta(days=1),
            )
        assert first is True and second is True
        async with factory() as session:
            names = set(
                await session.scalars(
                    select(AdvisorStrategy.name).where(
                        AdvisorStrategy.name.like("summary-%")
                    )
                )
            )
        assert names == {
            f"summary-{job.strategy_id}-2026-10-19",
            f"summary-{job.strategy_id}-2026-10-20",
        }
    finally:
        async with factory() as session, session.begin():
            await _wipe_summaries(session)
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_operator_can_correct_and_a_goal_expires_the_coupon() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            coupon_id = await _seed(session)
        async with factory() as session, session.begin():
            await accept_coupon(session, coupon_id, NOW, 180)
        async with factory() as session, session.begin():
            below = await modify_coupon(
                session,
                coupon_id,
                NOW,
                stake=Decimal("20.00"),
                odds={880_954: Decimal("1.200")},
                remove=set(),
                ttl_seconds=180,
            )
        assert below is True
        async with factory() as session, session.begin():
            with pytest.raises(ActionError, match="expired"):
                await place_coupon(session, coupon_id, NOW)
            coupon = await session.get(AdvisorCoupon, coupon_id)
        assert coupon is not None and coupon.status == "expired"
        async with factory() as session, session.begin():
            with pytest.raises(ActionError, match="expired"):
                await place_coupon(session, coupon_id, NOW)
            again = await session.get(AdvisorCoupon, coupon_id)
        assert again is not None and again.status == "expired"
        async with factory() as session, session.begin():
            strategy_id = await session.scalar(
                select(AdvisorStrategy.id).where(AdvisorStrategy.name == "reco-llm")
            )
            assert strategy_id is not None
            with pytest.raises(ActionError, match="sample"):
                await promote_strategy(session, strategy_id)
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


async def _seed(session) -> int:
    session.add(League(id=880_951, name="advisor-reco"))
    session.add(Team(id=880_952, name="Home"))
    session.add(Team(id=880_953, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=880_954,
            league_id=880_951,
            season=2026,
            home_team_id=880_952,
            away_team_id=880_953,
            status_short="2H",
            goals_home=1,
            goals_away=1,
            date=NOW - timedelta(hours=1),
        )
    )
    await session.flush()
    strategy = AdvisorStrategy(name="reco-llm", kind="llm", status="sandbox")
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
        candidate_rules=["favorite_losing", "tie_deficit"],
    )
    session.add(version)
    await session.flush()
    coupon = AdvisorCoupon(
        mode="recommendation",
        strategy_version_id=version.id,
        status="proposed",
        stake=Decimal("20.00"),
        proposed_stake=Decimal("20.00"),
        combined_odd=Decimal("1.700"),
        proposed_combined_odd=Decimal("1.700"),
        payout_keep=PAYOUT_KEEP,
        created_at=NOW,
    )
    session.add(coupon)
    await session.flush()
    session.add(
        AdvisorLeg(
            coupon_id=coupon.id,
            strategy_id=strategy.id,
            fixture_id=880_954,
            mode="recommendation",
            period="regulation",
            n=1,
            odd=Decimal("1.700"),
            proposed_odd=Decimal("1.700"),
            goals_home=0,
            goals_away=1,
            status="open",
        )
    )
    return coupon.id


async def _wipe_summaries(session) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(
        AdvisorStrategy.name.like("summary-%")
    )
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    await session.execute(
        delete(AdvisorCycle).where(AdvisorCycle.strategy_version_id.in_(version_ids))
    )
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
    source_versions = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(
            select(AdvisorStrategy.id).where(AdvisorStrategy.name == "reco-llm")
        )
    )
    await session.execute(
        delete(AdvisorCycle).where(
            AdvisorCycle.strategy_version_id.in_(source_versions)
        )
    )


async def _wipe(session) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name == "reco-llm")
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    from predictor.models.advisor import AdvisorCouponEvent

    await session.execute(
        delete(AdvisorCouponEvent).where(AdvisorCouponEvent.coupon_id.in_(coupon_ids))
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
    await session.execute(delete(Fixture).where(Fixture.id == 880_954))
    await session.execute(delete(Team).where(Team.id.in_((880_952, 880_953))))
    await session.execute(delete(League).where(League.id == 880_951))
