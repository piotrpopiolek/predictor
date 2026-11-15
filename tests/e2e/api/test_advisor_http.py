"""Advisor mutations go through the status app: token, status codes, read-only GET."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.api.main import create_app
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorLeg,
    AdvisorSettlementEvent,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
    AdvisorTelegramOutbox,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.advisor.domain import Period
from predictor.services.advisor.recommend_ops import place_proposed
from predictor.services.advisor.sandbox import LegSnapshot, SandboxSlip

_NAME = "http-advisor-llm"
_LEAGUE = 991_501
_HOME = 991_502
_AWAY = 991_503
_FIXTURE = 991_504
_FIXTURE_B = 991_514

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class _Seed:
    coupon_id: int
    strategy_id: int
    leg_id: int


def _post(
    client: TestClient,
    path: str,
    *,
    fields: dict[str, str] | None = None,
) -> object:
    return client.post(path, data=dict(fields or {}), follow_redirects=False)


async def _wipe(session: AsyncSession) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name == _NAME)
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    ids = list(await session.scalars(coupon_ids))
    for coupon_id in ids:
        await session.execute(
            delete(AdvisorTelegramOutbox).where(
                cast(AdvisorTelegramOutbox.payload, String).like(
                    f"%/advisor/recommendations/{coupon_id}%"
                )
            )
        )
    await session.execute(
        delete(AdvisorSettlementEvent).where(
            AdvisorSettlementEvent.coupon_id.in_(coupon_ids)
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
    await session.execute(delete(Fixture).where(Fixture.id.in_((_FIXTURE, _FIXTURE_B))))
    await session.execute(delete(Team).where(Team.id.in_((_HOME, _AWAY))))
    await session.execute(delete(League).where(League.id == _LEAGUE))


async def _seed(
    session: AsyncSession,
    *,
    created_at: datetime,
    status: str = "proposed",
    fixtures: tuple[int, ...] = (_FIXTURE,),
) -> list[_Seed]:
    await _wipe(session)
    session.add(League(id=_LEAGUE, name="http-advisor"))
    session.add(Team(id=_HOME, name="Home"))
    session.add(Team(id=_AWAY, name="Away"))
    await session.flush()
    for fixture_id in fixtures:
        session.add(
            Fixture(
                id=fixture_id,
                league_id=_LEAGUE,
                season=2026,
                home_team_id=_HOME,
                away_team_id=_AWAY,
                status_short="2H",
                goals_home=0,
                goals_away=1,
                date=created_at,
            )
        )
    await session.flush()
    strategy = AdvisorStrategy(name=_NAME, kind="llm", status="sandbox")
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
        slips=tuple(
            SandboxSlip(
                (
                    LegSnapshot(
                        fixture_id=fixture_id,
                        period=Period.REGULATION.value,
                        goals_home=0,
                        goals_away=1,
                        odd=Decimal("1.700"),
                        n=1,
                    ),
                )
            )
            for fixture_id in fixtures
        ),
        cycle_id=None,
        stake=Decimal("20.00"),
        created_at=created_at,
    )
    assert len(ids) == len(fixtures)
    if status != "proposed":
        rows = (
            await session.scalars(
                select(AdvisorCoupon).where(AdvisorCoupon.id.in_(ids))
            )
        ).all()
        for row in rows:
            row.status = status
    seeded: list[_Seed] = []
    for coupon_id in ids:
        leg = await session.scalar(
            select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
        )
        assert leg is not None
        seeded.append(_Seed(coupon_id, int(strategy.id), int(leg.id)))
    return seeded


@asynccontextmanager
async def _database() -> AsyncIterator[SessionFactory]:
    engine = make_async_engine(load_settings())
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


async def _coupon(factory: SessionFactory, coupon_id: int) -> AdvisorCoupon:
    async with factory() as session:
        coupon = await session.get(AdvisorCoupon, coupon_id)
    assert coupon is not None
    return coupon


async def _events(factory: SessionFactory, coupon_id: int) -> int:
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AdvisorCouponEvent)
            .where(AdvisorCouponEvent.coupon_id == coupon_id)
        )
    return int(count or 0)


def _fresh() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_accept_posts_change_the_coupon() -> None:
    async with _database() as factory:
        async with factory() as session, session.begin():
            first, second = await _seed(
                session,
                created_at=_fresh(),
                fixtures=(_FIXTURE, _FIXTURE_B),
            )
        with TestClient(create_app()) as client:
            by_bearer = _post(
                client,
                f"/advisor/recommendations/{first.coupon_id}/accept",
            )
            by_form = _post(
                client,
                f"/advisor/recommendations/{second.coupon_id}/accept",
            )
        assert by_bearer.status_code == 303
        assert by_form.status_code == 303
        assert by_bearer.headers["location"].endswith(
            f"/advisor/recommendations/{first.coupon_id}"
        )
        assert by_form.headers["location"].endswith(
            f"/advisor/recommendations/{second.coupon_id}"
        )
        assert (await _coupon(factory, first.coupon_id)).status == "accepted"
        assert (await _coupon(factory, second.coupon_id)).status == "accepted"


@pytest.mark.asyncio
async def test_form_token_places_an_accepted_coupon() -> None:
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=_fresh(), status="accepted"))[0]
        with TestClient(create_app()) as client:
            response = _post(
                client,
                f"/advisor/recommendations/{seeded.coupon_id}/place",
            )
        assert response.status_code == 303
        coupon = await _coupon(factory, seeded.coupon_id)
        assert coupon.status == "placed"
        assert coupon.placed_at is not None


@pytest.mark.asyncio
async def test_place_of_an_expired_coupon_is_409_and_writes_nothing() -> None:
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=_fresh(), status="expired"))[0]
        events_before = await _events(factory, seeded.coupon_id)
        with TestClient(create_app()) as client:
            response = _post(
                client,
                f"/advisor/recommendations/{seeded.coupon_id}/place",
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "expired"
        coupon = await _coupon(factory, seeded.coupon_id)
        async with factory() as session:
            leg = await session.get(AdvisorLeg, seeded.leg_id)
        assert coupon.status == "expired"
        assert coupon.placed_at is None
        assert leg is not None and leg.status == "open"
        assert await _events(factory, seeded.coupon_id) == events_before


@pytest.mark.asyncio
async def test_place_past_ttl_is_409_and_does_not_place() -> None:
    stale = _fresh() - timedelta(seconds=181)
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=stale, status="accepted"))[0]
        with TestClient(create_app()) as client:
            response = _post(
                client,
                f"/advisor/recommendations/{seeded.coupon_id}/place",
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "expired"
        coupon = await _coupon(factory, seeded.coupon_id)
        async with factory() as session:
            leg = await session.get(AdvisorLeg, seeded.leg_id)
        assert coupon.status == "expired"
        assert coupon.placed_at is None
        assert leg is not None and leg.status == "removed"


@pytest.mark.asyncio
async def test_get_pages_do_not_expire_or_settle() -> None:
    stale = _fresh() - timedelta(minutes=10)
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=stale, status="accepted"))[0]
        events_before = await _events(factory, seeded.coupon_id)
        with TestClient(create_app()) as client:
            listing = client.get("/advisor/recommendations")
            card = client.get(f"/advisor/recommendations/{seeded.coupon_id}")
            report = client.get("/advisor/report")
            report_json = client.get("/advisor/report.json")
        assert listing.status_code == 200
        assert card.status_code == 200
        assert report.status_code == 200
        assert report_json.status_code == 200
        assert 'name="token"' not in listing.text
        assert 'name="token"' not in card.text
        assert 'name="token"' not in report.text
        assert "accepted" in card.text
        coupon = await _coupon(factory, seeded.coupon_id)
        async with factory() as session:
            leg = await session.get(AdvisorLeg, seeded.leg_id)
        assert coupon.status == "accepted"
        assert coupon.placed_at is None
        assert coupon.stake == Decimal("20.00")
        assert leg is not None and leg.status == "open"
        assert await _events(factory, seeded.coupon_id) == events_before


@pytest.mark.asyncio
async def test_modify_rejects_a_bad_form_and_stores_a_valid_one() -> None:
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=_fresh()))[0]
        with TestClient(create_app()) as client:
            bad = _post(
                client,
                f"/advisor/recommendations/{seeded.coupon_id}/modify",
                fields={"stake": "abc", "odds": f"{_FIXTURE}=1.50"},
            )
            assert bad.status_code == 400
            assert bad.json()["detail"] == "invalid_form"
            coupon = await _coupon(factory, seeded.coupon_id)
            assert coupon.status == "proposed"
            good = _post(
                client,
                f"/advisor/recommendations/{seeded.coupon_id}/modify",
                fields={"stake": "20.00", "odds": f"{_FIXTURE}=1.50"},
            )
        assert good.status_code == 303
        coupon = await _coupon(factory, seeded.coupon_id)
        async with factory() as session:
            leg = await session.get(AdvisorLeg, seeded.leg_id)
        assert coupon.status == "modified"
        assert coupon.stake == Decimal("20.00")
        assert leg is not None and leg.odd == Decimal("1.500")


@pytest.mark.asyncio
async def test_missing_coupon_is_404_and_pause_resume_round_trip() -> None:
    async with _database() as factory:
        async with factory() as session, session.begin():
            seeded = (await _seed(session, created_at=_fresh()))[0]
        with TestClient(create_app()) as client:
            missing = _post(
                client,
                "/advisor/recommendations/991599/accept",
            )
            assert missing.status_code == 404
            assert missing.json()["detail"] == "missing"
            paused = _post(
                client,
                f"/advisor/strategies/{seeded.strategy_id}/pause",
            )
            assert paused.status_code == 303
            assert paused.headers["location"].endswith("/advisor/strategies")
            resumed = _post(
                client,
                f"/advisor/strategies/{seeded.strategy_id}/resume",
            )
            blocked = _post(
                client,
                f"/advisor/strategies/{seeded.strategy_id}/promote",
            )
        assert resumed.status_code == 303
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "sample"
        async with factory() as session:
            strategy = await session.get(AdvisorStrategy, seeded.strategy_id)
            coupon = await session.get(AdvisorCoupon, seeded.coupon_id)
        assert strategy is not None and strategy.status == "sandbox"
        assert coupon is not None and coupon.status == "proposed"
