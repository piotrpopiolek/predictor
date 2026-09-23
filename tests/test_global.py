from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.constants import (
    GLOBAL_ENDPOINT_ORDER,
    LOOKUP_WITHOUT_HTTP,
    TEAM_STATS_SENTINEL_DATE,
    TOP_PLAYER_ENDPOINTS,
)
from predictor.models.catalog import Coach, LeagueSeason, Player
from predictor.models.catalog_rest import CoachCareer, Sidelined, Transfer, Trophy
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team, Venue
from predictor.models.seasonal import (
    PlayerCareerTeam,
    PlayerStatistic,
    SquadMember,
    Standing,
    TeamSeasonStatistics,
)
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.global_entities import GlobalIngest
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import (
    enqueue_fixture_followups,
    enqueue_global_for_match,
    ensure_cursors,
    ensure_param_task,
    requeue_stale_global,
)


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
FIXTURE_ID = 9301
LEAGUE_ID = 39
SEASON = 2026
PLAYER_ID = 92001
PLAYER_B = 92002
COACH_ID = 7201
W8_ENDPOINTS = tuple(GLOBAL_ENDPOINT_ORDER) + tuple(LOOKUP_WITHOUT_HTTP)


def _envelope(response: Any, *, current: int = 1, total: int = 1) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _fixture_core() -> dict[str, Any]:
    return {
        "fixture": {
            "id": FIXTURE_ID,
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
            "periods": {"first": None, "second": None},
            "venue": {"id": 556, "name": "Stadium", "city": "London"},
            "status": {
                "long": "Not Started",
                "short": "NS",
                "elapsed": None,
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
            "home": {"id": 33, "name": "Home FC", "logo": None, "winner": None},
            "away": {"id": 34, "name": "Away FC", "logo": None, "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _standing_row(team_id: int, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "team": {"id": team_id, "name": f"Club {team_id}", "logo": None},
        "points": 10 - rank,
        "goalsDiff": 5 - rank,
        "group": "Premier League",
        "form": "WWD",
        "status": "same",
        "description": None,
        "update": "2026-09-13T00:00:00+00:00",
        "all": {
            "played": 4,
            "win": 3,
            "draw": 1,
            "lose": 0,
            "goals": {"for": 8, "against": 3},
        },
        "home": {
            "played": 2,
            "win": 2,
            "draw": 0,
            "lose": 0,
            "goals": {"for": 5, "against": 1},
        },
        "away": {
            "played": 2,
            "win": 1,
            "draw": 1,
            "lose": 0,
            "goals": {"for": 3, "against": 2},
        },
    }


def _standings_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "league": {
            "id": LEAGUE_ID,
            "name": "Premier League",
            "country": "England",
            "season": SEASON,
            "standings": [list(rows)],
        }
    }


def _venue(venue_id: int) -> dict[str, Any]:
    return {
        "id": venue_id,
        "name": "Old Trafford",
        "address": "Sir Matt Busby Way",
        "city": "Manchester",
        "country": "England",
        "capacity": 74310,
        "surface": "grass",
        "image": "https://example.test/venue.png",
    }


def _team_envelope(team_id: int, venue_id: int = 556) -> dict[str, Any]:
    return {
        "team": {
            "id": team_id,
            "name": f"Club {team_id}",
            "code": "CLB",
            "country": "England",
            "founded": 1878,
            "national": False,
            "logo": "https://example.test/logo.png",
        },
        "venue": _venue(venue_id),
    }


def _team_stats(team_id: int) -> dict[str, Any]:
    return {
        "league": {
            "id": LEAGUE_ID,
            "name": "Premier League",
            "country": "England",
            "season": SEASON,
        },
        "team": {"id": team_id, "name": f"Club {team_id}"},
        "form": "WWDLW",
        "fixtures": {
            "played": {"home": 4, "away": 4, "total": 8},
            "wins": {"home": 3, "away": 2, "total": 5},
            "draws": {"home": 1, "away": 1, "total": 2},
            "loses": {"home": 0, "away": 1, "total": 1},
        },
        "goals": {
            "for": {
                "total": {"home": 8, "away": 6, "total": 14},
                "average": {"home": "2.0", "away": "1.5", "total": "1.8"},
            },
            "against": {
                "total": {"home": 2, "away": 4, "total": 6},
                "average": {"home": "0.5", "away": "1.0", "total": "0.8"},
            },
        },
    }


def _player_bundle(player_id: int = PLAYER_ID) -> dict[str, Any]:
    return {
        "player": {
            "id": player_id,
            "name": f"Player {player_id}",
            "firstname": "Terry",
            "lastname": "Striker",
            "age": 28,
            "birth": {
                "date": "1998-01-15",
                "place": "London",
                "country": "England",
            },
            "nationality": "England",
            "height": "180 cm",
            "weight": "75 kg",
            "injured": False,
            "photo": "https://example.test/p.png",
        },
        "statistics": [
            {
                "team": {"id": 33, "name": "Home FC"},
                "league": {
                    "id": LEAGUE_ID,
                    "name": "Premier League",
                    "country": "England",
                    "season": SEASON,
                },
                "games": {
                    "appearences": 4,
                    "lineups": 4,
                    "minutes": 360,
                    "number": 9,
                    "position": "Attacker",
                    "rating": "7.20",
                    "captain": False,
                },
                "substitutes": {"in": 0, "out": 0, "bench": 1},
                "shots": {"total": 10, "on": 5},
                "goals": {"total": 3, "conceded": 0, "assists": 1, "saves": None},
                "passes": {"total": 80, "key": 5, "accuracy": 82},
                "tackles": {"total": 2, "blocks": 0, "interceptions": 1},
                "duels": {"total": 20, "won": 12},
                "dribbles": {"attempts": 8, "success": 4, "past": 1},
                "fouls": {"drawn": 3, "committed": 2},
                "cards": {"yellow": 1, "yellowred": 0, "red": 0},
                "penalty": {
                    "won": 0,
                    "commited": 0,
                    "scored": 1,
                    "missed": 0,
                    "saved": None,
                },
            }
        ],
    }


def _squad(team_id: int) -> dict[str, Any]:
    return {
        "team": {"id": team_id, "name": f"Club {team_id}"},
        "players": [
            {
                "id": PLAYER_ID,
                "name": "T. Striker",
                "age": 28,
                "number": 9,
                "position": "Attacker",
                "photo": None,
            }
        ],
    }


def _career() -> dict[str, Any]:
    return {"team": {"id": 33, "name": "Home FC"}, "seasons": [2024, 2026]}


def _coach() -> dict[str, Any]:
    return {
        "id": COACH_ID,
        "name": "A. Manager",
        "firstname": "Alex",
        "lastname": "Manager",
        "age": 50,
        "birth": {
            "date": "1976-02-01",
            "place": "Glasgow",
            "country": "Scotland",
        },
        "nationality": "Scotland",
        "height": None,
        "weight": None,
        "photo": None,
        "team": {"id": 33, "name": "Home FC"},
        "career": [
            {"team": {"id": 33, "name": "Home FC"}, "start": "2018", "end": None}
        ],
    }


def _transfer() -> dict[str, Any]:
    return {
        "player": {"id": PLAYER_ID, "name": "T. Striker"},
        "update": "2026-09-01T00:00:00+00:00",
        "transfers": [
            {
                "date": "2024-07-01",
                "type": "Free",
                "teams": {
                    "in": {"id": 33, "name": "Home FC"},
                    "out": {"id": 34, "name": "Away FC"},
                },
            }
        ],
    }


def _trophy() -> dict[str, Any]:
    return {
        "league": "Premier League",
        "country": "England",
        "season": "2023/2024",
        "place": "Winner",
    }


def _sidelined() -> dict[str, Any]:
    return {"type": "Suspended", "start": "2026-09-01", "end": "Unknown"}


def _client(settings: Settings, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


def _router(
    paths: list[str],
    *,
    standings: list[dict[str, Any]] | None = None,
    empty: frozenset[str] = frozenset(),
    player_pages: list[list[dict[str, Any]]] | None = None,
) -> Any:
    first_page: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params)
        paths.append(f"{request.url.path}?{query}" if query else request.url.path)
        path = request.url.path
        params = request.url.params
        if "ids" in params:
            raise AssertionError(f"ids= is Free-blocked: {request.url}")
        if path == "/coaches":
            raise AssertionError("coach catalog uses /coachs")
        if path in LOOKUP_WITHOUT_HTTP:
            raise AssertionError(f"lookup must not be fetched: {path}")
        if path not in first_page:
            first_page.add(path)
            if "page" in params:
                raise AssertionError(f"first request sent page= for {path}")
        if path in empty:
            return httpx.Response(200, json=_envelope([]))
        if path == "/standings":
            body = (
                standings
                if standings is not None
                else [_standings_payload(_standing_row(33, 1), _standing_row(34, 2))]
            )
            return httpx.Response(200, json=_envelope(body))
        if path == "/teams":
            team_id = int(params.get("id") or "33")
            return httpx.Response(200, json=_envelope([_team_envelope(team_id)]))
        if path == "/venues":
            venue_id = int(params.get("id") or "556")
            return httpx.Response(200, json=_envelope([_venue(venue_id)]))
        if path == "/teams/statistics":
            assert params.get("league") == str(LEAGUE_ID)
            assert params.get("season") == str(SEASON)
            assert "date" not in params
            team_id = int(params.get("team") or "33")
            return httpx.Response(200, json=_envelope(_team_stats(team_id)))
        if path == "/players":
            pages = player_pages or [[_player_bundle()]]
            page = int(params.get("page") or "1")
            index = page - 1
            body = pages[index] if 0 <= index < len(pages) else []
            return httpx.Response(
                200, json=_envelope(body, current=page, total=len(pages))
            )
        if path == "/players/profiles":
            return httpx.Response(200, json=_envelope([_player_bundle()]))
        if path == "/players/squads":
            team_id = int(params.get("team") or "33")
            return httpx.Response(200, json=_envelope([_squad(team_id)]))
        if path == "/players/teams":
            return httpx.Response(200, json=_envelope([_career()]))
        if path in TOP_PLAYER_ENDPOINTS:
            return httpx.Response(200, json=_envelope([_player_bundle()]))
        if path == "/coachs":
            assert params.get("id") == str(COACH_ID)
            return httpx.Response(200, json=_envelope([_coach()]))
        if path == "/transfers":
            return httpx.Response(200, json=_envelope([_transfer()]))
        if path == "/trophies":
            return httpx.Response(200, json=_envelope([_trophy()]))
        if path == "/sidelined":
            return httpx.Response(200, json=_envelope([_sidelined()]))
        raise AssertionError(f"unexpected {request.url}")

    return handler


async def _reset_w8(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(EtlTask)
                .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                .where(EtlTask.cursor_kind.is_(None))
            )
            await session.execute(
                delete(Sidelined).where(
                    or_(
                        Sidelined.player_id.in_((PLAYER_ID, PLAYER_B)),
                        Sidelined.coach_id == COACH_ID,
                    )
                )
            )
            await session.execute(
                delete(Trophy).where(
                    or_(
                        Trophy.player_id.in_((PLAYER_ID, PLAYER_B)),
                        Trophy.coach_id == COACH_ID,
                    )
                )
            )
            await session.execute(
                delete(Transfer).where(Transfer.player_id.in_((PLAYER_ID, PLAYER_B)))
            )
            await session.execute(
                delete(CoachCareer).where(CoachCareer.coach_id == COACH_ID)
            )
            await session.execute(
                delete(SquadMember).where(
                    SquadMember.player_id.in_((PLAYER_ID, PLAYER_B))
                )
            )
            await session.execute(
                delete(PlayerCareerTeam).where(
                    PlayerCareerTeam.player_id.in_((PLAYER_ID, PLAYER_B))
                )
            )
            await session.execute(
                delete(PlayerStatistic).where(
                    PlayerStatistic.player_id.in_((PLAYER_ID, PLAYER_B))
                )
            )
            await session.execute(
                delete(TeamSeasonStatistics).where(
                    TeamSeasonStatistics.league_id == LEAGUE_ID,
                    TeamSeasonStatistics.season == SEASON,
                )
            )
            await session.execute(
                delete(Standing).where(
                    Standing.league_id == LEAGUE_ID, Standing.season == SEASON
                )
            )
            await session.execute(delete(Fixture).where(Fixture.id == FIXTURE_ID))
            await ensure_cursors(session)


async def _seed(
    factory: async_sessionmaker[AsyncSession], **coverage: bool | None
) -> None:
    item = FixtureItem.model_validate(_fixture_core())
    async with factory() as session:
        async with session.begin():
            extra = await upsert_fixtures(session, [item])
            await enqueue_fixture_followups(session, extra)
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            if row is not None:
                row.cov_standings = True
                row.cov_players = True
                row.cov_top_scorers = True
                row.cov_top_assists = True
                row.cov_top_cards = True
                for name, value in coverage.items():
                    setattr(row, name, value)


@pytest.mark.asyncio
async def test_standings_replace_current_snapshot() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    client: FootballClient | None = None
    try:
        await _reset_w8(factory)
        await _seed(factory)
        paths: list[str] = []
        client = _client(
            settings,
            httpx.MockTransport(
                _router(
                    paths,
                    standings=[
                        _standings_payload(_standing_row(33, 1), _standing_row(34, 2))
                    ],
                )
            ),
        )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(Standing)
                    .where(Standing.league_id == LEAGUE_ID)
                    .order_by(Standing.rank)
                )
            )
        assert [row.team_id for row in rows] == [33, 34]
        assert rows[0].rank == 1
        await client.aclose()

        await _reset_w8(factory)
        await _seed(factory)
        paths = []
        client = _client(
            settings,
            httpx.MockTransport(
                _router(paths, standings=[_standings_payload(_standing_row(33, 4))])
            ),
        )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(Standing).where(Standing.league_id == LEAGUE_ID)
                )
            )
        assert len(rows) == 1
        assert rows[0].team_id == 33
        assert rows[0].rank == 4
        assert any(p.startswith("/standings") for p in paths)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_team_statistics_sentinel_omits_date() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(
                    session,
                    "/teams/statistics",
                    {"team": 33, "league": LEAGUE_ID, "season": SEASON},
                )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            row = await session.get(
                TeamSeasonStatistics,
                (33, LEAGUE_ID, SEASON, date.fromisoformat(TEAM_STATS_SENTINEL_DATE)),
            )
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/teams/statistics")
            )
        assert row is not None
        assert row.form == "WWDLW"
        assert row.played_total == 8
        assert task is not None
        assert task.status == "complete"
        assert task.params.get("as_of_date") == TEAM_STATS_SENTINEL_DATE
        assert paths
        assert all("date=" not in p for p in paths)
        assert all(p.startswith("/teams/statistics") for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_teams_and_venues_fill_catalog() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(session, "/teams", {"id": 33})
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=2)
        await ingest.refresh_pending()
        async with factory() as session:
            team = await session.get(Team, 33)
            venue = await session.get(Venue, 556)
            venue_task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/venues")
            )
        assert team is not None
        assert team.code == "CLB"
        assert team.founded == 1878
        assert team.venue_id == 556
        assert venue is not None
        assert venue.address == "Sir Matt Busby Way"
        assert venue.capacity == 74310
        assert venue_task is not None
        assert venue_task.status == "complete"
        assert any(p.startswith("/teams?") for p in paths)
        assert any(p.startswith("/venues?") for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_players_pages_and_catalog_followups() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(
        settings,
        httpx.MockTransport(
            _router(
                paths,
                player_pages=[[_player_bundle(PLAYER_ID)], [_player_bundle(PLAYER_B)]],
            )
        ),
    )
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(
                    session, "/players", {"league": LEAGUE_ID, "season": SEASON}
                )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            first = await session.get(Player, PLAYER_ID)
            second = await session.get(Player, PLAYER_B)
            stats = list(
                await session.scalars(
                    select(PlayerStatistic).where(
                        PlayerStatistic.player_id.in_((PLAYER_ID, PLAYER_B))
                    )
                )
            )
            followups = list(
                await session.scalars(
                    select(EtlTask.endpoint).where(
                        EtlTask.endpoint.in_(
                            (
                                "/players/profiles",
                                "/players/teams",
                                "/transfers",
                                "/trophies",
                                "/sidelined",
                            )
                        )
                    )
                )
            )
        assert first is not None and first.nationality == "England"
        assert second is not None
        assert len(stats) == 2
        assert stats[0].appearences == 4
        assert stats[0].sub_in == 0
        assert set(followups) >= {
            "/players/profiles",
            "/players/teams",
            "/transfers",
            "/trophies",
            "/sidelined",
        }
        player_gets = [p for p in paths if p.startswith("/players?")]
        assert player_gets
        assert "page=" not in player_gets[0]
        assert any("page=2" in p for p in player_gets)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_coachs_career_and_xor_catalog() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(session, "/coachs", {"id": COACH_ID})
                await ensure_param_task(session, "/transfers", {"player": PLAYER_ID})
                await ensure_param_task(session, "/trophies", {"player": PLAYER_ID})
                await ensure_param_task(session, "/sidelined", {"player": PLAYER_ID})
                await ensure_param_task(session, "/players/squads", {"team": 33})
                await ensure_param_task(
                    session, "/players/teams", {"player": PLAYER_ID}
                )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=10)
        await ingest.refresh_pending()
        async with factory() as session:
            coach = await session.get(Coach, COACH_ID)
            career = await session.get(CoachCareer, (COACH_ID, 33, date(2018, 1, 1)))
            squad = await session.get(SquadMember, (33, PLAYER_ID))
            seasons = list(
                await session.scalars(
                    select(PlayerCareerTeam.season).where(
                        PlayerCareerTeam.player_id == PLAYER_ID
                    )
                )
            )
            moves = list(
                await session.scalars(
                    select(Transfer).where(Transfer.player_id == PLAYER_ID)
                )
            )
            cups = list(
                await session.scalars(
                    select(Trophy).where(Trophy.player_id == PLAYER_ID)
                )
            )
            bans = list(
                await session.scalars(
                    select(Sidelined).where(Sidelined.player_id == PLAYER_ID)
                )
            )
        assert coach is not None
        assert coach.firstname == "Alex"
        assert career is not None
        assert career.end_date is None
        assert squad is not None
        assert squad.position == "Attacker"
        assert sorted(seasons) == [2024, 2026]
        assert len(moves) == 1
        assert moves[0].type == "Free"
        assert moves[0].to_team_id == 33
        assert cups[0].place == "Winner"
        assert bans[0].end_date is None
        assert bans[0].type == "Suspended"
        assert any(p.startswith("/coachs?") for p in paths)
        assert not any(p.startswith("/coaches") for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_topscorers_use_player_statistics() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(
                    session,
                    "/players/topscorers",
                    {"league": LEAGUE_ID, "season": SEASON},
                )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            row = await session.scalar(
                select(PlayerStatistic).where(
                    PlayerStatistic.player_id == PLAYER_ID,
                    PlayerStatistic.league_id == LEAGUE_ID,
                    PlayerStatistic.season == SEASON,
                )
            )
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/players/topscorers")
            )
        assert row is not None
        assert row.goals == 3
        assert task is not None
        assert task.status == "complete"
        assert paths
        assert all(p.startswith("/players/topscorers") for p in paths)
        assert "page=" not in paths[0]
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconstructable_lookups_skip_http() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                session.add(
                    EtlTask(
                        endpoint="/teams/seasons",
                        params={"team": 33},
                        status="pending",
                    )
                )
                session.add(
                    EtlTask(
                        endpoint="/players/seasons",
                        params={"player": PLAYER_ID},
                        status="pending",
                    )
                )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=3)
        await ingest.refresh_pending()
        async with factory() as session:
            tasks = list(
                await session.scalars(
                    select(EtlTask).where(
                        EtlTask.endpoint.in_(tuple(LOOKUP_WITHOUT_HTTP))
                    )
                )
            )
        assert paths == []
        assert {task.endpoint: task.status for task in tasks} == {
            "/teams/seasons": "not_supported",
            "/players/seasons": "not_supported",
        }
        assert all(task.last_error == "reconstructable_lookup" for task in tasks)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_coverage_false_and_empty_are_terminal() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    client: FootballClient | None = None
    try:
        await _reset_w8(factory)
        await _seed(factory, cov_standings=False)
        paths: list[str] = []
        client = _client(settings, httpx.MockTransport(_router(paths)))
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/standings")
            )
        assert task is not None
        assert task.status == "coverage_empty"
        assert paths == []
        await client.aclose()

        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    delete(EtlTask)
                    .where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                    .where(EtlTask.cursor_kind.is_(None))
                )
                await ensure_param_task(
                    session, "/standings", {"league": LEAGUE_ID, "season": SEASON}
                )
        paths = []
        client = _client(
            settings,
            httpx.MockTransport(_router(paths, empty=frozenset({"/standings"}))),
        )
        ingest = GlobalIngest(client, factory, now_fn=lambda: NOW, per_tick=1)
        await ingest.refresh_pending()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/standings")
            )
        assert task is not None
        assert task.status == "coverage_empty"
        assert any(p.startswith("/standings") for p in paths)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_fixture_followups_skip_season_lookups() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset_w8(factory)
        await _seed(factory)
        async with factory() as session:
            endpoints = set(
                await session.scalars(
                    select(EtlTask.endpoint).where(EtlTask.endpoint.in_(W8_ENDPOINTS))
                )
            )
        assert "/standings" in endpoints
        assert "/players" in endpoints
        assert "/teams" in endpoints
        assert "/players/squads" in endpoints
        assert "/teams/statistics" in endpoints
        assert set(TOP_PLAYER_ENDPOINTS) <= endpoints
        assert "/teams/seasons" not in endpoints
        assert "/players/seasons" not in endpoints
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_global_requeues_next_utc_day() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        await _reset_w8(factory)
        async with factory() as session:
            async with session.begin():
                session.add(
                    EtlTask(
                        endpoint="/standings",
                        params={"league": LEAGUE_ID, "season": SEASON},
                        status="complete",
                        completed_at=NOW - timedelta(days=1),
                    )
                )
            async with session.begin():
                count = await requeue_stale_global(session, NOW)
                task = await session.scalar(
                    select(EtlTask).where(EtlTask.endpoint == "/standings")
                )
        assert count == 1
        assert task is not None
        assert task.status == "pending"
        assert task.created_at is not None
        created = task.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        assert created == NOW
    finally:
        await engine.dispose()


def test_enqueue_global_helper_exports() -> None:
    assert callable(enqueue_global_for_match)
    assert TEAM_STATS_SENTINEL_DATE == "1970-01-01"
