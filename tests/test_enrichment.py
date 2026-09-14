from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.constants import GLOBAL_ENDPOINT_ORDER, LOOKUP_WITHOUT_HTTP
from predictor.models.catalog import LeagueSeason, Player
from predictor.models.children import (
    FixtureEvent,
    FixtureLineup,
    FixtureLineupPlayer,
    FixturePlayerStats,
    FixtureStatistic,
)
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team
from predictor.models.injuries import Injury
from predictor.models.odds import FixtureOdds, OddsFixtureMapping
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.enrichment import FixtureDetail
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.enrichment import EnrichmentIngest
from predictor.services.ingest.persist_children import nested_fixture_teams
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import (
    ensure_cursors,
    ensure_enrichment_task,
    ensure_injuries_task,
)


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
FT_ID = 8001
PST_ID = 8002
NS_ID = 8003
LEAGUE_ID = 39
SEASON = 2026


def _envelope(response: Any, *, current: int = 1, total: int = 1) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _fixture_core(
    fixture_id: int,
    *,
    status: str,
    extra_event: bool = False,
) -> dict[str, Any]:
    long_status = {
        "FT": "Match Finished",
        "PST": "Match Postponed",
        "NS": "Not Started",
    }.get(status, status)
    body: dict[str, Any] = {
        "fixture": {
            "id": fixture_id,
            "referee": "M. Oliver",
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
            "periods": {"first": 1789311600, "second": 1789315200},
            "venue": {"id": 556, "name": "Stadium", "city": "London"},
            "status": {
                "long": long_status,
                "short": status,
                "elapsed": 90 if status == "FT" else None,
                "extra": None,
            },
        },
        "league": {
            "id": LEAGUE_ID,
            "name": "Premier League",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": SEASON,
            "round": "Regular Season - 4",
        },
        "teams": {
            "home": {"id": 33, "name": "Home FC", "logo": None, "winner": True},
            "away": {"id": 34, "name": "Away FC", "logo": None, "winner": False},
        },
        "goals": {"home": 1 if status == "FT" else None, "away": 0},
        "score": {
            "halftime": {"home": 1 if status == "FT" else None, "away": 0},
            "fulltime": {"home": 1 if status == "FT" else None, "away": 0},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }
    if extra_event:
        body["events"] = [_event_payload(), _event_payload()]
    return body


def _event_payload() -> dict[str, Any]:
    return {
        "time": {"elapsed": 12, "extra": None},
        "team": {"id": 33, "name": "Home FC"},
        "player": {"id": 90001, "name": "Striker"},
        "assist": {"id": 90002, "name": "Winger"},
        "type": "Goal",
        "detail": "Normal Goal",
        "comments": None,
    }


def _lineup_payload(team_id: int, coach_id: int, player_id: int) -> dict[str, Any]:
    return {
        "team": {
            "id": team_id,
            "name": "Club",
            "colors": {"player": {"primary": "ff0"}},
        },
        "coach": {"id": coach_id, "name": "Coach"},
        "formation": "4-3-3",
        "startXI": [
            {
                "player": {
                    "id": player_id,
                    "name": "Starter",
                    "number": 9,
                    "pos": "F",
                    "grid": "1:1",
                }
            }
        ],
        "substitutes": [
            {
                "player": {
                    "id": player_id + 1,
                    "name": "Bench",
                    "number": 18,
                    "pos": "M",
                    "grid": None,
                }
            }
        ],
    }


def _id_payload(fixture_id: int = FT_ID) -> dict[str, Any]:
    body = _fixture_core(fixture_id, status="FT")
    body["events"] = [_event_payload()]
    body["lineups"] = [
        _lineup_payload(33, 7001, 90001),
        _lineup_payload(34, 7002, 90101),
    ]
    body["statistics"] = [
        {
            "team": {"id": 33, "name": "Home FC"},
            "statistics": [{"type": "Shots on Goal", "value": 5}],
        }
    ]
    body["players"] = [
        {
            "team": {
                "id": 33,
                "name": "Home FC",
                "update": "2026-09-13T17:00:00+00:00",
            },
            "players": [
                {
                    "player": {"id": 90001, "name": "Striker"},
                    "statistics": [
                        {
                            "games": {
                                "minutes": 90,
                                "number": 9,
                                "position": "F",
                                "rating": "7.3",
                                "captain": False,
                                "substitute": False,
                            },
                            "offsides": 1,
                            "shots": {"total": 3, "on": 2},
                            "goals": {
                                "total": 1,
                                "conceded": 0,
                                "assists": 0,
                                "saves": 0,
                            },
                            "passes": {"total": 20, "key": 2, "accuracy": "80"},
                            "tackles": {"total": 1, "blocks": 0, "interceptions": 0},
                            "duels": {"total": 8, "won": 5},
                            "dribbles": {"attempts": 2, "success": 1, "past": 0},
                            "fouls": {"drawn": 1, "committed": 0},
                            "cards": {"yellow": 0, "red": 0},
                            "penalty": {
                                "won": 0,
                                "commited": 0,
                                "scored": 0,
                                "missed": 0,
                                "saved": 0,
                            },
                        }
                    ],
                }
            ],
        },
        {
            "team": {
                "id": 26594,
                "name": "Guest FC",
                "update": "2026-09-13T17:00:00+00:00",
            },
            "players": [
                {
                    "player": {"id": 91001, "name": "Guest"},
                    "statistics": [
                        {"games": {"minutes": 12, "position": "M"}},
                    ],
                }
            ],
        },
    ]
    return body


def test_nested_fixture_teams_include_player_block_club() -> None:
    detail = FixtureDetail.model_validate(_id_payload())
    assert {team.id for team in nested_fixture_teams(detail)} >= {33, 34, 26594}


def _half_payload() -> dict[str, Any]:
    return {
        "team": {"id": 33, "name": "Home FC"},
        "statistics": [{"type": "Should not persist as FT", "value": 99}],
        "statistics_1H": [{"type": "Shots on Goal", "value": 3}],
        "statistics_2H": [{"type": "Shots on Goal", "value": 2}],
    }


def _injury_payload(*, fixture_id: int = FT_ID) -> dict[str, Any]:
    return {
        "player": {
            "id": 90001,
            "name": "Striker",
            "type": "Missing Fixture",
            "reason": "Knee Injury",
        },
        "team": {"id": 33, "name": "Home FC"},
        "fixture": {"id": fixture_id, "timezone": "UTC"},
        "league": {"id": LEAGUE_ID, "season": SEASON, "name": "Premier League"},
    }


def _router(
    paths: list[str],
    *,
    extra: dict[str, Any] | None = None,
) -> Any:
    state = extra if extra is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params)
        paths.append(f"{request.url.path}?{query}" if query else request.url.path)
        path = request.url.path
        params = request.url.params
        if "ids" in params:
            raise AssertionError(f"ids= is Free-blocked: {request.url}")
        if path == "/fixtures" and params.get("id") == str(FT_ID):
            payload = _id_payload()
            if state.get("event_twice"):
                payload["events"] = [_event_payload(), _event_payload()]
            if state.get("duplicate_player_stats"):
                group = payload["players"][0]["players"]
                payload["players"][0]["players"] = [*group, *group]
            return httpx.Response(200, json=_envelope([payload]))
        if path == "/fixtures/statistics" and params.get("half") == "true":
            return httpx.Response(200, json=_envelope([_half_payload()]))
        if path == "/injuries":
            return httpx.Response(200, json=_envelope([_injury_payload()]))
        raise AssertionError(f"unexpected {request.url}")

    return handler


def _client(settings: Settings, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


async def _reset_w6(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(EtlTask)
                .where(
                    EtlTask.endpoint.in_(
                        (
                            "/fixtures",
                            "/fixtures/statistics",
                            "/injuries",
                            *GLOBAL_ENDPOINT_ORDER,
                            *LOOKUP_WITHOUT_HTTP,
                        )
                    )
                )
                .where(EtlTask.cursor_kind.is_(None))
                .where(EtlTask.day_utc.is_(None))
            )
            await session.execute(delete(Injury))
            await session.execute(
                delete(PredictionH2H).where(
                    or_(
                        PredictionH2H.fixture_id.in_((FT_ID, PST_ID, NS_ID)),
                        PredictionH2H.h2h_fixture_id.in_((FT_ID, PST_ID, NS_ID)),
                    )
                )
            )
            await session.execute(
                delete(Prediction).where(
                    Prediction.fixture_id.in_((FT_ID, PST_ID, NS_ID))
                )
            )
            await session.execute(
                delete(FixtureOdds).where(
                    FixtureOdds.fixture_id.in_((FT_ID, PST_ID, NS_ID))
                )
            )
            await session.execute(
                delete(OddsFixtureMapping).where(
                    OddsFixtureMapping.fixture_id.in_((FT_ID, PST_ID, NS_ID))
                )
            )
            await session.execute(delete(FixtureEvent))
            await session.execute(delete(FixtureLineupPlayer))
            await session.execute(delete(FixtureLineup))
            await session.execute(delete(FixtureStatistic))
            await session.execute(delete(FixturePlayerStats))
            await session.execute(
                delete(Fixture).where(Fixture.id.in_((FT_ID, PST_ID, NS_ID)))
            )
            await ensure_cursors(session)


async def _seed_fixture(
    factory: async_sessionmaker[AsyncSession],
    fixture_id: int,
    *,
    status: str,
    enrichment: bool = True,
    injuries: bool = False,
) -> None:
    detail = FixtureDetail.model_validate(_fixture_core(fixture_id, status=status))
    async with factory() as session:
        async with session.begin():
            await upsert_fixtures(session, [detail])
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            if row is not None:
                row.cov_events = True
                row.cov_lineups = True
                row.cov_statistics_fixtures = True
                row.cov_statistics_players = True
                row.cov_injuries = True
            if enrichment:
                await ensure_enrichment_task(session, fixture_id)
            if injuries:
                await ensure_injuries_task(session, LEAGUE_ID, SEASON)


async def _set_coverage(
    factory: async_sessionmaker[AsyncSession],
    **flags: bool | None,
) -> None:
    async with factory() as session:
        async with session.begin():
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            assert row is not None
            for name, value in flags.items():
                setattr(row, name, value)


@pytest.mark.asyncio
async def test_id_enrichment_persists_children_and_half_stats() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, FT_ID, status="FT")
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW, per_tick=6)
        await ingest.refresh_pending()

        async with factory() as session:
            events = await session.scalar(
                select(func.count())
                .select_from(FixtureEvent)
                .where(FixtureEvent.fixture_id == FT_ID)
            )
            lineups = await session.scalar(
                select(func.count())
                .select_from(FixtureLineup)
                .where(FixtureLineup.fixture_id == FT_ID)
            )
            lineup_players = await session.scalar(
                select(func.count())
                .select_from(FixtureLineupPlayer)
                .where(FixtureLineupPlayer.fixture_id == FT_ID)
            )
            ft_shots = await session.scalar(
                select(FixtureStatistic).where(
                    FixtureStatistic.fixture_id == FT_ID,
                    FixtureStatistic.period == "FT",
                    FixtureStatistic.stat_type == "Shots on Goal",
                )
            )
            half_1h = await session.scalar(
                select(FixtureStatistic).where(
                    FixtureStatistic.fixture_id == FT_ID,
                    FixtureStatistic.period == "1H",
                    FixtureStatistic.stat_type == "Shots on Goal",
                )
            )
            half_2h = await session.scalar(
                select(FixtureStatistic).where(
                    FixtureStatistic.fixture_id == FT_ID,
                    FixtureStatistic.period == "2H",
                )
            )
            leaked_ft = await session.scalar(
                select(FixtureStatistic).where(
                    FixtureStatistic.fixture_id == FT_ID,
                    FixtureStatistic.period == "FT",
                    FixtureStatistic.stat_type == "Should not persist as FT",
                )
            )
            player_stats = await session.scalar(
                select(FixturePlayerStats).where(
                    FixturePlayerStats.fixture_id == FT_ID,
                    FixturePlayerStats.player_id == 90001,
                )
            )
            guest_stats = await session.scalar(
                select(FixturePlayerStats).where(
                    FixturePlayerStats.fixture_id == FT_ID,
                    FixturePlayerStats.player_id == 91001,
                )
            )
            guest_team = await session.get(Team, 26594)
            player = await session.get(Player, 90001)
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == FT_ID)
            )
            half_task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures/statistics")
                .where(EtlTask.fixture_id == FT_ID)
            )
            cursor = await session.scalar(
                select(EtlTask).where(EtlTask.cursor_kind == "enrichment")
            )

        assert events == 1
        assert lineups == 2
        assert lineup_players == 4
        assert ft_shots is not None and ft_shots.stat_value == "5"
        assert half_1h is not None and half_1h.stat_value == "3"
        assert half_2h is not None and half_2h.stat_value == "2"
        assert leaked_ft is None
        assert player_stats is not None
        assert player_stats.goals == 1
        assert player_stats.pen_committed == 0
        assert guest_stats is not None
        assert guest_team is not None
        assert guest_team.name == "Guest FC"
        assert player is not None
        assert task is not None
        assert task.status == "complete"
        assert task.params.get("control_done") is False
        due = datetime.fromisoformat(task.params["control_due"])
        assert due == NOW + timedelta(hours=24)
        assert half_task is not None
        assert half_task.status == "complete"
        assert cursor is not None
        assert cursor.params.get("last_fixture_id") == FT_ID
        assert any(p.startswith("/fixtures?id=") for p in paths)
        assert any("half=true" in p for p in paths)
        assert not any("ids=" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_irregular_is_coverage_empty_without_http() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, PST_ID, status="PST")
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == PST_ID)
            )
            events = await session.scalar(
                select(func.count()).select_from(FixtureEvent)
            )
        assert task is not None
        assert task.status == "coverage_empty"
        assert task.params.get("reason") == "irregular_status"
        assert events == 0
        assert paths == []
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ns_is_not_claimed() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, NS_ID, status="NS")
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == NS_ID)
            )
        assert task is not None
        assert task.status == "pending"
        assert paths == []
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_refresh_after_due_replaces_events() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    later = NOW + timedelta(hours=25)
    clock = {"t": NOW}
    extra: dict[str, Any] = {"event_twice": False}

    def now() -> datetime:
        return clock["t"]

    client = _client(settings, httpx.MockTransport(_router(paths, extra=extra)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, FT_ID, status="FT")
        ingest = EnrichmentIngest(client, factory, now_fn=now, per_tick=6)
        await ingest.refresh_pending()
        async with factory() as session:
            first_events = await session.scalar(
                select(func.count())
                .select_from(FixtureEvent)
                .where(FixtureEvent.fixture_id == FT_ID)
            )
        async with factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == FT_ID)
                )
                assert task is not None
                params = dict(task.params)
                params["control_due"] = NOW.isoformat()
                params["control_done"] = False
                task.params = params
                task.status = "complete"
        assert first_events == 1
        extra["event_twice"] = True
        clock["t"] = later
        await ingest.refresh_finalization()
        async with factory() as session:
            events = await session.scalar(
                select(func.count())
                .select_from(FixtureEvent)
                .where(FixtureEvent.fixture_id == FT_ID)
            )
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == FT_ID)
            )
        assert events == 2
        assert task is not None
        assert task.status == "complete"
        assert task.params.get("control_done") is True
        assert sum(1 for p in paths if p.startswith("/fixtures?id=")) == 2
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_events_replace_is_idempotent() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    client = _client(settings, httpx.MockTransport(_router([])))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, FT_ID, status="FT")
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW, per_tick=6)
        await ingest.refresh_pending()
        async with factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == FT_ID)
                )
                assert task is not None
                task.status = "pending"
        await ingest.refresh_pending()
        async with factory() as session:
            events = await session.scalar(
                select(func.count())
                .select_from(FixtureEvent)
                .where(FixtureEvent.fixture_id == FT_ID)
            )
        assert events == 1
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_injuries_league_season_upsert() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(
            factory, FT_ID, status="NS", enrichment=False, injuries=True
        )
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            row = await session.scalar(select(Injury))
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/injuries")
            )
        assert row is not None
        assert row.player_id == 90001
        assert row.fixture_id == FT_ID
        assert row.reason == "Knee Injury"
        assert task is not None
        assert task.status == "complete"
        assert any(p.startswith("/injuries?") for p in paths)
        assert any("league=39" in p and "season=2026" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_injuries_coverage_false_skips_http() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(
            factory, NS_ID, status="NS", enrichment=False, injuries=True
        )
        await _set_coverage(factory, cov_injuries=False)
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/injuries")
            )
            n = await session.scalar(select(func.count()).select_from(Injury))
        assert task is not None
        assert task.status == "coverage_empty"
        assert task.params.get("reason") == "coverage_false"
        assert n == 0
        assert paths == []
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_all_children_coverage_false_still_gets_id() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, FT_ID, status="FT")
        await _set_coverage(
            factory,
            cov_events=False,
            cov_lineups=False,
            cov_statistics_fixtures=False,
            cov_statistics_players=False,
        )
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            events = await session.scalar(
                select(func.count()).select_from(FixtureEvent)
            )
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == FT_ID)
            )
            half_task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/fixtures/statistics")
            )
        assert events == 0
        assert task is not None
        assert task.status == "coverage_empty"
        assert half_task is None
        assert any(p.startswith("/fixtures?id=") for p in paths)
        assert not any("half=true" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_player_stats_do_not_fail_persist() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    extra: dict[str, Any] = {"duplicate_player_stats": True}
    client = _client(settings, httpx.MockTransport(_router([], extra=extra)))
    try:
        await _reset_w6(factory)
        await _seed_fixture(factory, FT_ID, status="FT")
        ingest = EnrichmentIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_pending()
        async with factory() as session:
            n = await session.scalar(
                select(func.count())
                .select_from(FixturePlayerStats)
                .where(FixturePlayerStats.fixture_id == FT_ID)
            )
            strikers = await session.scalar(
                select(func.count())
                .select_from(FixturePlayerStats)
                .where(FixturePlayerStats.fixture_id == FT_ID)
                .where(FixturePlayerStats.player_id == 90001)
            )
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == FT_ID)
            )
        assert n == 2
        assert strikers == 1
        assert task is not None
        assert task.status == "complete"
        assert task.params.get("counts", {}).get("player_stats") == 2
    finally:
        await client.aclose()
        await engine.dispose()
