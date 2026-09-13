from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.quota import QuotaSnapshot
from predictor.constants import DAILY_REPORT_ENDPOINT
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import load_settings
from predictor.services.completeness import (
    day_is_complete,
    match_blockers,
    match_is_complete,
    write_daily_report,
)
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import (
    complete_task,
    ensure_cursors,
    ensure_enrichment_task,
    ensure_h2h_task,
    ensure_half_stats_task,
    ensure_odds_task,
    ensure_predictions_task,
)
from predictor.services.scheduler import Scheduler

DAY = date(2099, 1, 2)
EMPTY_DAY = date(2099, 1, 1)
REPORT_DAY = date(2026, 9, 12)
FT_ID = 99101
NS_ID = 99102
PST_ID = 99103
HOME_ID = 960
AWAY_ID = 961
LEAGUE_ID = 39
SEASON = 2026
NOW = datetime(2026, 9, 13, 0, 5, tzinfo=UTC)


def _fixture_payload(
    fixture_id: int,
    *,
    status: str,
    kickoff: str,
) -> dict[str, Any]:
    long_status = {
        "FT": "Match Finished",
        "NS": "Not Started",
        "PST": "Match Postponed",
    }.get(status, status)
    return {
        "fixture": {
            "id": fixture_id,
            "referee": "M. Oliver",
            "timezone": "UTC",
            "date": kickoff,
            "timestamp": 1789052400,
            "periods": {"first": None, "second": None},
            "venue": {"id": 610, "name": "Park", "city": "London"},
            "status": {"long": long_status, "short": status, "elapsed": None},
        },
        "league": {
            "id": LEAGUE_ID,
            "name": "Premier League",
            "country": "England",
            "season": SEASON,
            "round": "Regular Season - 1",
        },
        "teams": {
            "home": {"id": HOME_ID, "name": "Home FC", "winner": None},
            "away": {"id": AWAY_ID, "name": "Away FC", "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


async def _reset(factory: async_sessionmaker[AsyncSession]) -> None:
    ids = (FT_ID, NS_ID, PST_ID)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(EtlTask)
                .where(EtlTask.endpoint == DAILY_REPORT_ENDPOINT)
                .where(EtlTask.day_utc.in_((DAY, EMPTY_DAY, REPORT_DAY)))
            )
            await session.execute(
                delete(EtlTask)
                .where(EtlTask.endpoint == "/standings")
                .where(EtlTask.params.contains({"season": 2099}))
            )
            await session.execute(
                delete(EtlTask)
                .where(EtlTask.endpoint == "/fixtures/headtohead")
                .where(EtlTask.params.contains({"h2h": f"{HOME_ID}-{AWAY_ID}"}))
            )
            await session.execute(
                delete(EtlTask).where(EtlTask.endpoint == "/w9/report-error")
            )
            await session.execute(
                delete(EtlTask).where(EtlTask.day_utc.in_((DAY, EMPTY_DAY)))
            )
            await session.execute(delete(EtlTask).where(EtlTask.fixture_id.in_(ids)))
            await session.execute(delete(Fixture).where(Fixture.id.in_(ids)))
            await ensure_cursors(session)


async def _mark_day_fetched(
    session: AsyncSession,
    day: date,
    *,
    paging_current: int = 1,
    paging_total: int = 1,
) -> None:
    session.add(
        EtlTask(
            endpoint="/fixtures",
            day_utc=day,
            params={"date": day.isoformat(), "count": 0},
            status="complete",
            paging_current=paging_current,
            paging_total=paging_total,
        )
    )


async def _seed_fixture(
    factory: async_sessionmaker[AsyncSession],
    fixture_id: int,
    *,
    status: str,
    kickoff: str,
) -> None:
    item = FixtureItem.model_validate(
        _fixture_payload(fixture_id, status=status, kickoff=kickoff)
    )
    async with factory() as session:
        async with session.begin():
            await upsert_fixtures(session, [item])


@pytest.mark.asyncio
async def test_empty_fetched_day_is_complete() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, EMPTY_DAY)
                assert await day_is_complete(session, EMPTY_DAY) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_paging_is_not_complete() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(
                    session, EMPTY_DAY, paging_current=1, paging_total=2
                )
                assert await day_is_complete(session, EMPTY_DAY) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ns_open_children_do_not_block_the_day() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        await _seed_fixture(
            factory,
            NS_ID,
            status="NS",
            kickoff="2099-01-02T15:00:00+00:00",
        )
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, DAY)
                await ensure_enrichment_task(session, NS_ID)
                await ensure_predictions_task(session, NS_ID)
                fixture = await session.get(Fixture, NS_ID)
                assert fixture is not None
                assert await match_blockers(session, fixture) == []
                assert await match_is_complete(session, fixture) is True
                assert await day_is_complete(session, DAY) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ft_pending_enrichment_blocks_the_day() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        await _seed_fixture(
            factory,
            FT_ID,
            status="FT",
            kickoff="2099-01-02T18:00:00+00:00",
        )
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, DAY)
                await ensure_enrichment_task(session, FT_ID)
                fixture = await session.get(Fixture, FT_ID)
                assert fixture is not None
                assert "/fixtures" in await match_blockers(session, fixture)
                assert await day_is_complete(session, DAY) is False
                enrichment = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == FT_ID)
                )
                assert enrichment is not None
                await complete_task(session, enrichment, "coverage_empty")
                session.add(
                    EtlTask(
                        endpoint="/standings",
                        params={"league": LEAGUE_ID, "season": 2099},
                        status="pending",
                    )
                )
                assert await day_is_complete(session, DAY) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ft_open_half_and_predictions_block() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        await _seed_fixture(
            factory,
            FT_ID,
            status="FT",
            kickoff="2099-01-02T18:00:00+00:00",
        )
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, DAY)
                await ensure_enrichment_task(session, FT_ID)
                enrichment = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == FT_ID)
                )
                assert enrichment is not None
                await complete_task(session, enrichment, "complete")
                await ensure_half_stats_task(session, FT_ID)
                await ensure_predictions_task(session, FT_ID)
                await ensure_odds_task(session, FT_ID)
                await ensure_h2h_task(session, HOME_ID, AWAY_ID)
                fixture = await session.get(Fixture, FT_ID)
                assert fixture is not None
                blockers = await match_blockers(session, fixture)
                assert "/fixtures/statistics" in blockers
                assert "/predictions" in blockers
                assert "/odds" in blockers
                assert "/fixtures/headtohead" in blockers
                assert await day_is_complete(session, DAY) is False
                pair = f"{HOME_ID}-{AWAY_ID}"
                for task in await session.scalars(
                    select(EtlTask).where(
                        EtlTask.endpoint.in_(
                            (
                                "/fixtures/statistics",
                                "/predictions",
                                "/odds",
                                "/fixtures/headtohead",
                            )
                        )
                    )
                ):
                    if task.status != "pending":
                        continue
                    if task.fixture_id == FT_ID or (
                        task.endpoint == "/fixtures/headtohead"
                        and task.params.get("h2h") == pair
                    ):
                        await complete_task(session, task, "not_supported")
                assert await day_is_complete(session, DAY) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_irregular_coverage_empty_is_complete_without_half() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset(factory)
        await _seed_fixture(
            factory,
            PST_ID,
            status="PST",
            kickoff="2099-01-02T12:00:00+00:00",
        )
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, DAY)
                await ensure_enrichment_task(session, PST_ID)
                enrichment = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == PST_ID)
                )
                assert enrichment is not None
                await complete_task(session, enrichment, "coverage_empty")
                fixture = await session.get(Fixture, PST_ID)
                assert fixture is not None
                assert await match_blockers(session, fixture) == []
                assert await day_is_complete(session, DAY) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_daily_report_is_idempotent_and_records_quota() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    quota = QuotaSnapshot(
        current=10,
        limit_day=100,
        remaining=90,
        fetched_at=NOW,
        source="api",
    )
    try:
        await _reset(factory)
        async with factory() as session:
            async with session.begin():
                await _mark_day_fetched(session, DAY)
                live = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/odds/live")
                    .where(EtlTask.cursor_kind.is_(None))
                    .order_by(EtlTask.id)
                    .limit(1)
                )
                if live is None:
                    session.add(
                        EtlTask(
                            endpoint="/odds/live",
                            params={"last_poll_at": "2026-09-10T23:59:00+00:00"},
                            status="complete",
                        )
                    )
                else:
                    live.params = {
                        **dict(live.params),
                        "last_poll_at": "2026-09-10T23:59:00+00:00",
                    }
                session.add(
                    EtlTask(
                        endpoint="/w9/report-error",
                        params={},
                        status="retryable_error",
                        last_error="boom",
                    )
                )
                first = await write_daily_report(session, DAY, quota=quota, now=NOW)
                second = await write_daily_report(session, DAY, quota=quota, now=NOW)
        assert first is True
        assert second is False
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(EtlTask)
                    .where(EtlTask.endpoint == DAILY_REPORT_ENDPOINT)
                    .where(EtlTask.day_utc == DAY)
                )
            )
        assert len(rows) == 1
        report = rows[0]
        assert report.status == "complete"
        assert report.params["day"] == DAY.isoformat()
        assert report.params["complete"] is True
        assert report.params["quota"]["remaining"] == 90
        assert report.params["live_last_poll_at"] == "2026-09-10T23:59:00+00:00"
        assert any(
            item["endpoint"] == "/w9/report-error" for item in report.params["errors"]
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_writes_yesterday_report_once() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)

    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            return QuotaSnapshot(
                current=1,
                limit_day=100,
                remaining=99,
                fetched_at=NOW,
                source="api",
            )

    try:
        await _reset(factory)
        scheduler = Scheduler(
            settings,
            cast(Any, _Client()),
            factory,
            idle_cap_seconds=0.01,
            now_fn=lambda: NOW,
        )
        await scheduler._tick(asyncio.Event())
        await scheduler._tick(asyncio.Event())
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(EtlTask)
                    .where(EtlTask.endpoint == DAILY_REPORT_ENDPOINT)
                    .where(EtlTask.day_utc == REPORT_DAY)
                )
            )
        assert len(rows) == 1
        assert rows[0].status == "complete"
        assert rows[0].params["day"] == "2026-09-12"
    finally:
        await engine.dispose()
