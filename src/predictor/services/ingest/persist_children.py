"""Idempotent persist for fixture children and injuries (W6)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import Coach, Player
from predictor.models.children import (
    FixtureEvent,
    FixtureLineup,
    FixtureLineupPlayer,
    FixturePlayerStats,
    FixtureStatistic,
)
from predictor.models.fixtures import Fixture
from predictor.models.injuries import Injury
from predictor.schemas.enrichment import (
    FixtureDetail,
    FixtureEventItem,
    InjuryItem,
    LineupItem,
    LineupPlayerWrap,
    PlayerStatBlock,
    StatKV,
    TeamPlayersItem,
    TeamStatisticsItem,
)
from predictor.services.ingest.persist import _chunks
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import enqueue_coach_catalog


async def upsert_player_stubs(
    session: AsyncSession,
    items: Sequence[tuple[int, str | None, str | None]],
) -> None:
    unique: dict[int, tuple[int, str | None, str | None]] = {
        player_id: (player_id, name, photo) for player_id, name, photo in items
    }
    if not unique:
        return
    for chunk in _chunks(list(unique.values())):
        values = [
            {"id": player_id, "name": name, "photo": photo}
            for player_id, name, photo in chunk
        ]
        stmt = insert(Player).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Player.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Player.name),
                "photo": func.coalesce(stmt.excluded.photo, Player.photo),
            },
        )
        await session.execute(stmt)


async def upsert_coach_stubs(
    session: AsyncSession,
    items: Sequence[tuple[int, str | None, str | None]],
) -> None:
    unique: dict[int, tuple[int, str | None, str | None]] = {
        coach_id: (coach_id, name, photo) for coach_id, name, photo in items
    }
    if not unique:
        return
    for chunk in _chunks(list(unique.values())):
        values = [
            {"id": coach_id, "name": name, "photo": photo}
            for coach_id, name, photo in chunk
        ]
        stmt = insert(Coach).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Coach.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Coach.name),
                "photo": func.coalesce(stmt.excluded.photo, Coach.photo),
            },
        )
        await session.execute(stmt)


def _stat_value(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw)
    return text[:50]


def _as_str(raw: object, limit: int) -> str | None:
    if raw is None:
        return None
    return str(raw)[:limit]


async def persist_fixture_detail(
    session: AsyncSession,
    detail: FixtureDetail,
    *,
    include_events: bool,
    include_lineups: bool,
    include_statistics: bool,
    include_players: bool,
) -> dict[str, int]:
    extra = await upsert_fixtures(session, [detail])
    fixture_id = detail.fixture.id
    fixture = await session.get(Fixture, fixture_id)
    home_id = fixture.home_team_id if fixture is not None else None
    players: list[tuple[int, str | None, str | None]] = []
    coaches: list[tuple[int, str | None, str | None]] = []
    _collect_people(detail, players, coaches)
    await upsert_player_stubs(session, players)
    await upsert_coach_stubs(session, coaches)
    for coach_id in dict.fromkeys(cid for cid, _, _ in coaches):
        await enqueue_coach_catalog(session, coach_id)
    counts = {
        "events": 0,
        "lineups": 0,
        "lineup_players": 0,
        "statistics": 0,
        "player_stats": 0,
        "fixture": int(extra.get("count", 0)),
    }
    if include_events:
        counts["events"] = await _replace_events(session, fixture_id, detail.events)
    if include_lineups:
        lineups, lineup_players = await _upsert_lineups(
            session, fixture_id, home_id, detail.lineups
        )
        counts["lineups"] = lineups
        counts["lineup_players"] = lineup_players
    if include_statistics:
        counts["statistics"] = await upsert_team_statistics(
            session, fixture_id, detail.statistics, default_period="FT"
        )
    if include_players:
        counts["player_stats"] = await _upsert_player_stats(
            session, fixture_id, detail.players
        )
    return counts


def _collect_people(
    detail: FixtureDetail,
    players: list[tuple[int, str | None, str | None]],
    coaches: list[tuple[int, str | None, str | None]],
) -> None:
    for event in detail.events:
        if event.player is not None and event.player.id is not None:
            players.append((event.player.id, event.player.name, event.player.photo))
        if event.assist is not None and event.assist.id is not None:
            players.append((event.assist.id, event.assist.name, event.assist.photo))
    for lineup in detail.lineups:
        if lineup.coach is not None and lineup.coach.id is not None:
            coaches.append((lineup.coach.id, lineup.coach.name, lineup.coach.photo))
        for wrap in list(lineup.start_xi) + list(lineup.substitutes):
            person = wrap.player
            if person is not None and person.id is not None:
                players.append((person.id, person.name, person.photo))
    for group in detail.players:
        for row in group.players:
            if row.player is not None and row.player.id is not None:
                players.append((row.player.id, row.player.name, row.player.photo))


async def _replace_events(
    session: AsyncSession,
    fixture_id: int,
    events: Sequence[FixtureEventItem],
) -> int:
    await session.execute(
        delete(FixtureEvent).where(FixtureEvent.fixture_id == fixture_id)
    )
    rows: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if event.team is None or event.team.id is None:
            continue
        minute = event.time.elapsed if event.time is not None else None
        if minute is None or not event.type:
            continue
        extra = event.time.extra if event.time is not None else None
        rows.append(
            {
                "fixture_id": fixture_id,
                "team_id": event.team.id,
                "player_id": event.player.id if event.player is not None else None,
                "assist_player_id": (
                    event.assist.id if event.assist is not None else None
                ),
                "event_type": event.type[:16],
                "detail": event.detail[:100] if event.detail else None,
                "minute": minute,
                "minute_extra": extra,
                "comments": event.comments,
                "sort_order": index,
            }
        )
    if not rows:
        return 0
    for chunk in _chunks(rows):
        await session.execute(insert(FixtureEvent).values(list(chunk)))
    return len(rows)


async def _upsert_lineups(
    session: AsyncSession,
    fixture_id: int,
    home_id: int | None,
    lineups: Sequence[LineupItem],
) -> tuple[int, int]:
    lineup_rows: list[dict[str, Any]] = []
    player_rows: list[dict[str, Any]] = []
    for lineup in lineups:
        if lineup.team is None or lineup.team.id is None:
            continue
        team_id = lineup.team.id
        lineup_rows.append(
            {
                "fixture_id": fixture_id,
                "team_id": team_id,
                "coach_id": lineup.coach.id if lineup.coach is not None else None,
                "formation": lineup.formation,
                "is_home": home_id is not None and team_id == home_id,
                "colors": lineup.team.colors,
            }
        )
        player_rows.extend(
            _lineup_player_rows(fixture_id, team_id, lineup.start_xi, starter=True)
        )
        player_rows.extend(
            _lineup_player_rows(fixture_id, team_id, lineup.substitutes, starter=False)
        )
    unique_lineups: dict[tuple[int, int], dict[str, Any]] = {}
    for row in lineup_rows:
        unique_lineups[(row["fixture_id"], row["team_id"])] = row
    lineup_rows = list(unique_lineups.values())
    unique_players: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in player_rows:
        unique_players[(row["fixture_id"], row["team_id"], row["player_id"])] = row
    player_rows = list(unique_players.values())
    if lineup_rows:
        for chunk in _chunks(lineup_rows):
            stmt = insert(FixtureLineup).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=[FixtureLineup.fixture_id, FixtureLineup.team_id],
                set_={
                    "coach_id": stmt.excluded.coach_id,
                    "formation": stmt.excluded.formation,
                    "is_home": stmt.excluded.is_home,
                    "colors": stmt.excluded.colors,
                },
            )
            await session.execute(stmt)
    if player_rows:
        for chunk in _chunks(player_rows):
            stmt = insert(FixtureLineupPlayer).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    FixtureLineupPlayer.fixture_id,
                    FixtureLineupPlayer.team_id,
                    FixtureLineupPlayer.player_id,
                ],
                set_={
                    "number": stmt.excluded.number,
                    "position": stmt.excluded.position,
                    "grid": stmt.excluded.grid,
                    "is_starter": stmt.excluded.is_starter,
                },
            )
            await session.execute(stmt)
    return len(lineup_rows), len(player_rows)


def _lineup_player_rows(
    fixture_id: int,
    team_id: int,
    wraps: Sequence[LineupPlayerWrap],
    *,
    starter: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for wrap in wraps:
        person = wrap.player
        if person is None or person.id is None:
            continue
        rows.append(
            {
                "fixture_id": fixture_id,
                "team_id": team_id,
                "player_id": person.id,
                "number": person.number,
                "position": (person.pos or None),
                "grid": person.grid,
                "is_starter": starter,
            }
        )
    return rows


async def upsert_team_statistics(
    session: AsyncSession,
    fixture_id: int,
    items: Sequence[TeamStatisticsItem],
    *,
    default_period: str,
    include_default: bool = True,
) -> int:
    rows: list[dict[str, Any]] = []
    for item in items:
        if item.team is None or item.team.id is None:
            continue
        team_id = item.team.id
        if include_default:
            rows.extend(
                _stat_rows(fixture_id, team_id, item.statistics, default_period)
            )
        rows.extend(_stat_rows(fixture_id, team_id, item.statistics_1h, "1H"))
        rows.extend(_stat_rows(fixture_id, team_id, item.statistics_2h, "2H"))
    unique_stats: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    for row in rows:
        unique_stats[
            (row["fixture_id"], row["team_id"], row["stat_type"], row["period"])
        ] = row
    rows = list(unique_stats.values())
    if not rows:
        return 0
    for chunk in _chunks(rows):
        stmt = insert(FixtureStatistic).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                FixtureStatistic.fixture_id,
                FixtureStatistic.team_id,
                FixtureStatistic.stat_type,
                FixtureStatistic.period,
            ],
            set_={"stat_value": stmt.excluded.stat_value},
        )
        await session.execute(stmt)
    return len(rows)


def _stat_rows(
    fixture_id: int,
    team_id: int,
    stats: Sequence[StatKV],
    period: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stat in stats:
        if not stat.type:
            continue
        rows.append(
            {
                "fixture_id": fixture_id,
                "team_id": team_id,
                "stat_type": stat.type[:100],
                "stat_value": _stat_value(stat.value),
                "period": period,
            }
        )
    return rows


async def _upsert_player_stats(
    session: AsyncSession,
    fixture_id: int,
    groups: Sequence[TeamPlayersItem],
) -> int:
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group.team is None or group.team.id is None:
            continue
        updated = group.team.update
        for entry in group.players:
            if entry.player is None or entry.player.id is None:
                continue
            block = entry.statistics[0] if entry.statistics else PlayerStatBlock()
            rows.append(
                _player_stat_row(
                    fixture_id,
                    group.team.id,
                    entry.player.id,
                    updated,
                    block,
                )
            )
    unique: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in rows:
        unique[(row["fixture_id"], row["team_id"], row["player_id"])] = row
    rows = list(unique.values())
    if not rows:
        return 0
    for chunk in _chunks(rows):
        values = list(chunk)
        stmt = insert(FixturePlayerStats).values(values)
        update_cols = {
            key: getattr(stmt.excluded, key)
            for key in values[0]
            if key not in {"fixture_id", "team_id", "player_id"}
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                FixturePlayerStats.fixture_id,
                FixturePlayerStats.team_id,
                FixturePlayerStats.player_id,
            ],
            set_=update_cols,
        )
        await session.execute(stmt)
    return len(rows)


def _player_stat_row(
    fixture_id: int,
    team_id: int,
    player_id: int,
    updated: datetime | None,
    block: PlayerStatBlock,
) -> dict[str, Any]:
    games = block.games
    shots = block.shots
    goals = block.goals
    passes = block.passes
    tackles = block.tackles
    duels = block.duels
    dribbles = block.dribbles
    fouls = block.fouls
    cards = block.cards
    penalty = block.penalty
    return {
        "fixture_id": fixture_id,
        "team_id": team_id,
        "player_id": player_id,
        "stats_updated_at": updated,
        "minutes": games.minutes if games is not None else None,
        "number": games.number if games is not None else None,
        "position": games.position if games is not None else None,
        "rating": _as_str(games.rating, 8) if games is not None else None,
        "captain": games.captain if games is not None else None,
        "substitute": games.substitute if games is not None else None,
        "offsides": block.offsides,
        "shots_total": shots.total if shots is not None else None,
        "shots_on": shots.on if shots is not None else None,
        "goals": goals.total if goals is not None else None,
        "goals_conceded": goals.conceded if goals is not None else None,
        "assists": goals.assists if goals is not None else None,
        "saves": goals.saves if goals is not None else None,
        "passes_total": passes.total if passes is not None else None,
        "passes_key": passes.key if passes is not None else None,
        "passes_accuracy": (
            _as_str(passes.accuracy, 16) if passes is not None else None
        ),
        "tackles": tackles.total if tackles is not None else None,
        "blocks": tackles.blocks if tackles is not None else None,
        "interceptions": tackles.interceptions if tackles is not None else None,
        "duels_total": duels.total if duels is not None else None,
        "duels_won": duels.won if duels is not None else None,
        "dribbles_attempts": dribbles.attempts if dribbles is not None else None,
        "dribbles_success": dribbles.success if dribbles is not None else None,
        "dribbles_past": dribbles.past if dribbles is not None else None,
        "fouls_drawn": fouls.drawn if fouls is not None else None,
        "fouls_committed": fouls.committed if fouls is not None else None,
        "yellow_cards": cards.yellow if cards is not None else None,
        "red_cards": cards.red if cards is not None else None,
        "pen_won": penalty.won if penalty is not None else None,
        "pen_committed": penalty.commited if penalty is not None else None,
        "pen_scored": penalty.scored if penalty is not None else None,
        "pen_missed": penalty.missed if penalty is not None else None,
        "pen_saved": penalty.saved if penalty is not None else None,
    }


async def persist_injuries(session: AsyncSession, items: Sequence[InjuryItem]) -> int:
    people: list[tuple[int, str | None, str | None]] = []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in items:
        if item.player is None or item.player.id is None:
            continue
        if item.team is None or item.team.id is None:
            continue
        if item.fixture is None or item.fixture.id is None:
            continue
        key = (item.fixture.id, item.player.id)
        if key in seen:
            continue
        seen.add(key)
        people.append((item.player.id, item.player.name, item.player.photo))
        rows.append(
            {
                "player_id": item.player.id,
                "team_id": item.team.id,
                "fixture_id": item.fixture.id,
                "league_id": item.league.id if item.league is not None else None,
                "season": item.league.season if item.league is not None else None,
                "availability": item.player.type,
                "reason": item.player.reason,
            }
        )
    await upsert_player_stubs(session, people)
    if not rows:
        return 0
    fixture_ids = {row["fixture_id"] for row in rows}
    existing = set(
        await session.scalars(select(Fixture.id).where(Fixture.id.in_(fixture_ids)))
    )
    rows = [row for row in rows if row["fixture_id"] in existing]
    if not rows:
        return 0
    for chunk in _chunks(rows):
        stmt = insert(Injury).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Injury.fixture_id, Injury.player_id],
            set_={
                "team_id": stmt.excluded.team_id,
                "league_id": stmt.excluded.league_id,
                "season": stmt.excluded.season,
                "availability": stmt.excluded.availability,
                "reason": stmt.excluded.reason,
            },
        )
        await session.execute(stmt)
    return len(rows)
