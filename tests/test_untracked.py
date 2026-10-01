from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from predictor.models.catalog import UntrackedLeague
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.fixtures import forecast_day_load
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import (
    drop_untracked_fixture_ids,
    enqueue_fixture_followups,
    ensure_prematch_for_fixture_ids,
    historical_backlog_blocks,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LEAGUE = 99061
TRACKED = 99072
IGNORED = 99071


def _payload(
    fixture_id: int, league_id: int, *, kickoff: datetime, status: str = "NS"
) -> dict[str, Any]:
    return {
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": kickoff.isoformat(),
            "timestamp": int(kickoff.timestamp()),
            "periods": {"first": None, "second": None},
            "venue": {"id": 1, "name": "S", "city": "C"},
            "status": {
                "long": status,
                "short": status,
                "elapsed": 12 if status == "1H" else None,
                "extra": None,
            },
        },
        "league": {
            "id": league_id,
            "name": "L",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": 2026,
            "round": "R1",
        },
        "teams": {
            "home": {"id": 33, "name": "Home", "logo": None, "winner": None},
            "away": {"id": 34, "name": "Away", "logo": None, "winner": None},
        },
        "goals": {"home": 0, "away": 0},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


async def _cleanup(factory: Any) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(EtlTask).where(EtlTask.fixture_id.in_((TRACKED, IGNORED)))
            )
            await session.execute(
                delete(EtlTask).where(EtlTask.params.contains({"league": LEAGUE}))
            )
            await session.execute(
                delete(EtlTask).where(EtlTask.params.contains({"marker": LEAGUE}))
            )
            await session.execute(
                delete(UntrackedLeague).where(UntrackedLeague.league_id == LEAGUE)
            )
            await session.execute(
                delete(Fixture).where(Fixture.id.in_((TRACKED, IGNORED)))
            )


@pytest.mark.asyncio
async def test_untracked_league_skips_followups_live_ids_and_open_tasks() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _cleanup(factory)
        async with factory() as session:
            async with session.begin():
                tracked = FixtureItem.model_validate(
                    _payload(TRACKED, 39, kickoff=NOW + timedelta(hours=3))
                )
                ignored = FixtureItem.model_validate(
                    _payload(IGNORED, LEAGUE, kickoff=NOW + timedelta(hours=3))
                )
                await upsert_fixtures(session, [tracked, ignored])
                session.add(UntrackedLeague(league_id=LEAGUE, note="test"))
                session.add(
                    EtlTask(
                        endpoint="/fixtures",
                        fixture_id=IGNORED,
                        params={"id": IGNORED},
                        status="pending",
                    )
                )
                session.add(
                    EtlTask(
                        endpoint="/standings",
                        params={"league": LEAGUE, "season": 2026},
                        status="pending",
                    )
                )
                session.add(
                    EtlTask(
                        endpoint="/fixtures",
                        params={"date": "2099-01-01", "marker": LEAGUE},
                        status="pending",
                    )
                )
                blocked = await historical_backlog_blocks(session, NOW)
                await enqueue_fixture_followups(
                    session,
                    {
                        "discovered": [
                            {
                                "id": TRACKED,
                                "home": 33,
                                "away": 34,
                                "league": 39,
                                "season": 2026,
                            },
                            {
                                "id": IGNORED,
                                "home": 33,
                                "away": 34,
                                "league": LEAGUE,
                                "season": 2026,
                            },
                        ],
                        "league_seasons": [
                            {"league": 39, "season": 2026},
                            {"league": LEAGUE, "season": 2026},
                        ],
                    },
                    historical=not blocked,
                )
                await enqueue_fixture_followups(
                    session,
                    {"fixture_ids": [TRACKED, IGNORED]},
                    historical=False,
                )
                kept = await drop_untracked_fixture_ids(session, [TRACKED, IGNORED])
                empty = await drop_untracked_fixture_ids(session, [])
        assert kept == [TRACKED]
        assert empty == []
        async with factory() as session:
            tracked_odds = await session.scalar(
                select(EtlTask.id)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == TRACKED)
            )
            ignored_odds = await session.scalar(
                select(EtlTask.id)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == IGNORED)
            )
            detail = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/fixtures",
                    EtlTask.fixture_id == IGNORED,
                )
            )
            standings = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/standings",
                    EtlTask.params.contains({"league": LEAGUE}),
                )
            )
            day_task = await session.scalar(
                select(EtlTask).where(EtlTask.params.contains({"marker": LEAGUE}))
            )
            rounds = await session.scalar(
                select(EtlTask.id).where(
                    EtlTask.endpoint == "/fixtures/rounds",
                    EtlTask.params.contains({"league": LEAGUE}),
                )
            )
        assert tracked_odds is not None
        assert ignored_odds is None
        assert rounds is None
        assert detail is not None and detail.status == "not_supported"
        assert detail.last_error == "untracked_league"
        assert standings is not None and standings.status == "not_supported"
        assert day_task is not None and day_task.status == "pending"
    finally:
        await _cleanup(factory)
        await engine.dispose()


@pytest.mark.asyncio
async def test_forecast_and_prematch_skip_untracked_league() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    kickoff = NOW + timedelta(hours=1)
    try:
        await _cleanup(factory)
        before = await forecast_day_load(factory, NOW)
        async with factory() as session:
            async with session.begin():
                tracked = FixtureItem.model_validate(
                    _payload(TRACKED, 39, kickoff=kickoff)
                )
                ignored = FixtureItem.model_validate(
                    _payload(IGNORED, LEAGUE, kickoff=kickoff)
                )
                await upsert_fixtures(session, [tracked, ignored])
                session.add(UntrackedLeague(league_id=LEAGUE, note="test"))
                await ensure_prematch_for_fixture_ids(session, [TRACKED, IGNORED])
        after = await forecast_day_load(factory, NOW)
        assert after.matches == before.matches + 1
        async with factory() as session:
            tracked_odds = await session.scalar(
                select(EtlTask.id)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == TRACKED)
            )
            ignored_odds = await session.scalar(
                select(EtlTask.id)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == IGNORED)
            )
        assert tracked_odds is not None
        assert ignored_odds is None
    finally:
        await _cleanup(factory)
        await engine.dispose()
