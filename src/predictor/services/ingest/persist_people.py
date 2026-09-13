"""Persist players, squads, career, coaches, transfers, trophies, sidelined."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import Coach, Player
from predictor.models.catalog_rest import CoachCareer, Sidelined, Transfer, Trophy
from predictor.models.seasonal import PlayerCareerTeam, PlayerStatistic, SquadMember
from predictor.schemas.global_entities import (
    CareerTeamItem,
    CoachItem,
    IdName,
    PersonCore,
    PlayerBundle,
    PlayerStatisticsBlock,
    SidelinedItem,
    SquadItem,
    TransferBundle,
    TrophyItem,
)
from predictor.services.ingest.persist import _chunks, upsert_seasons
from predictor.services.ingest.persist_children import upsert_player_stubs
from predictor.services.ingest.persist_seasonal import (
    as_str,
    parse_flexible_date,
    upsert_team_ids,
)


def _player_values(person: PersonCore) -> dict[str, Any]:
    birth = person.birth
    return {
        "id": person.id,
        "name": person.name,
        "firstname": person.firstname,
        "lastname": person.lastname,
        "age": person.age,
        "birth_date": birth.date if birth is not None else None,
        "birth_place": birth.place if birth is not None else None,
        "birth_country": birth.country if birth is not None else None,
        "nationality": person.nationality,
        "height": as_str(person.height, 20),
        "weight": as_str(person.weight, 20),
        "injured": person.injured,
        "photo": person.photo,
    }


async def upsert_player_profiles(
    session: AsyncSession, people: Sequence[PersonCore]
) -> int:
    unique: dict[int, dict[str, Any]] = {}
    for person in people:
        unique[person.id] = _player_values(person)
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = list(chunk)
        stmt = insert(Player).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Player.id],
            set_={
                key: func.coalesce(getattr(stmt.excluded, key), getattr(Player, key))
                for key in values[0]
                if key != "id"
            },
        )
        await session.execute(stmt)
        count += len(values)
    return count


def _stat_row(player_id: int, block: PlayerStatisticsBlock) -> dict[str, Any] | None:
    if block.team is None or block.team.id is None:
        return None
    if block.league is None or block.league.id is None or block.league.season is None:
        return None
    games = block.games
    subs = block.substitutes
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
        "player_id": player_id,
        "team_id": block.team.id,
        "league_id": block.league.id,
        "season": block.league.season,
        "appearences": games.appearences if games else None,
        "lineups": games.lineups if games else None,
        "minutes": games.minutes if games else None,
        "number": games.number if games else None,
        "position": as_str(games.position if games else None, 32),
        "rating": as_str(games.rating if games else None, 8),
        "captain": games.captain if games else None,
        "sub_in": subs.inn if subs else None,
        "sub_out": subs.out if subs else None,
        "sub_bench": subs.bench if subs else None,
        "shots_total": shots.total if shots else None,
        "shots_on": shots.on if shots else None,
        "goals": goals.total if goals else None,
        "goals_conceded": goals.conceded if goals else None,
        "assists": goals.assists if goals else None,
        "saves": goals.saves if goals else None,
        "passes_total": passes.total if passes else None,
        "passes_key": passes.key if passes else None,
        "passes_accuracy": as_str(passes.accuracy if passes else None, 16),
        "tackles": tackles.total if tackles else None,
        "blocks": tackles.blocks if tackles else None,
        "interceptions": tackles.interceptions if tackles else None,
        "duels_total": duels.total if duels else None,
        "duels_won": duels.won if duels else None,
        "dribbles_attempts": dribbles.attempts if dribbles else None,
        "dribbles_success": dribbles.success if dribbles else None,
        "dribbles_past": dribbles.past if dribbles else None,
        "fouls_drawn": fouls.drawn if fouls else None,
        "fouls_committed": fouls.committed if fouls else None,
        "yellow": cards.yellow if cards else None,
        "yellowred": cards.yellowred if cards else None,
        "red": cards.red if cards else None,
        "pen_won": penalty.won if penalty else None,
        "pen_committed": penalty.commited if penalty else None,
        "pen_scored": penalty.scored if penalty else None,
        "pen_missed": penalty.missed if penalty else None,
        "pen_saved": penalty.saved if penalty else None,
    }


async def persist_player_bundles(
    session: AsyncSession, items: Sequence[PlayerBundle]
) -> dict[str, Any]:
    people = [item.player for item in items]
    await upsert_player_profiles(session, people)
    teams: list[IdName] = []
    years: list[int] = []
    stats: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    player_ids: list[int] = []
    for item in items:
        player_ids.append(item.player.id)
        for block in item.statistics:
            if block.team is not None:
                teams.append(block.team)
            if block.league is not None and block.league.season is not None:
                years.append(block.league.season)
            row = _stat_row(item.player.id, block)
            if row is None:
                continue
            key = (row["player_id"], row["team_id"], row["league_id"], row["season"])
            stats[key] = row
    await upsert_seasons(session, years)
    await upsert_team_ids(session, teams)
    stored = 0
    if stats:
        for chunk in _chunks(list(stats.values())):
            values = list(chunk)
            stmt = insert(PlayerStatistic).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    PlayerStatistic.player_id,
                    PlayerStatistic.team_id,
                    PlayerStatistic.league_id,
                    PlayerStatistic.season,
                ],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in values[0]
                    if key not in {"player_id", "team_id", "league_id", "season"}
                },
            )
            await session.execute(stmt)
            stored += len(values)
    return {"players": len(people), "statistics": stored, "player_ids": player_ids}


async def persist_squads(session: AsyncSession, items: Sequence[SquadItem]) -> int:
    stubs: list[tuple[int, str | None, str | None]] = []
    teams: list[IdName] = []
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    for item in items:
        if item.team is None or item.team.id is None:
            continue
        teams.append(item.team)
        for player in item.players:
            if player.id is None:
                continue
            stubs.append((player.id, player.name, player.photo))
            rows[(item.team.id, player.id)] = {
                "team_id": item.team.id,
                "player_id": player.id,
                "number": player.number,
                "position": as_str(player.position, 32),
            }
    await upsert_team_ids(session, teams)
    await upsert_player_stubs(session, stubs)
    if not rows:
        return 0
    stored = 0
    for chunk in _chunks(list(rows.values())):
        values = list(chunk)
        stmt = insert(SquadMember).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SquadMember.team_id, SquadMember.player_id],
            set_={
                "number": stmt.excluded.number,
                "position": stmt.excluded.position,
            },
        )
        await session.execute(stmt)
        stored += len(values)
    return stored


async def persist_player_career(
    session: AsyncSession,
    player_id: int,
    items: Sequence[CareerTeamItem],
) -> int:
    teams: list[IdName] = []
    years: list[int] = []
    rows: dict[tuple[int, int, int], dict[str, Any]] = {}
    for item in items:
        if item.team is None or item.team.id is None:
            continue
        teams.append(item.team)
        for season in item.seasons:
            years.append(int(season))
            rows[(player_id, item.team.id, int(season))] = {
                "player_id": player_id,
                "team_id": item.team.id,
                "season": int(season),
            }
    await upsert_seasons(session, years)
    await upsert_team_ids(session, teams)
    if not rows:
        return 0
    stored = 0
    for chunk in _chunks(list(rows.values())):
        stmt = insert(PlayerCareerTeam).values(list(chunk))
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                PlayerCareerTeam.player_id,
                PlayerCareerTeam.team_id,
                PlayerCareerTeam.season,
            ]
        )
        await session.execute(stmt)
        stored += len(chunk)
    return stored


async def persist_coaches(session: AsyncSession, items: Sequence[CoachItem]) -> int:
    stored = 0
    for item in items:
        teams: list[IdName] = []
        if item.team is not None and item.team.id is not None:
            teams.append(item.team)
        career_rows: dict[tuple[int, int, Any], dict[str, Any]] = {}
        for stint in item.career:
            if stint.team is None or stint.team.id is None:
                continue
            teams.append(stint.team)
            start = parse_flexible_date(stint.start)
            if start is None:
                continue
            career_rows[(item.id, stint.team.id, start)] = {
                "coach_id": item.id,
                "team_id": stint.team.id,
                "start_date": start,
                "end_date": parse_flexible_date(stint.end),
            }
        await upsert_team_ids(session, teams)
        birth = item.birth
        values = {
            "id": item.id,
            "name": item.name,
            "firstname": item.firstname,
            "lastname": item.lastname,
            "age": item.age,
            "birth_date": birth.date if birth is not None else None,
            "birth_place": birth.place if birth is not None else None,
            "birth_country": birth.country if birth is not None else None,
            "nationality": item.nationality,
            "height": as_str(item.height, 20),
            "weight": as_str(item.weight, 20),
            "photo": item.photo,
            "team_id": item.team.id if item.team is not None else None,
        }
        stmt = insert(Coach).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Coach.id],
            set_={
                key: func.coalesce(getattr(stmt.excluded, key), getattr(Coach, key))
                for key in values
                if key != "id"
            },
        )
        await session.execute(stmt)
        await session.execute(
            delete(CoachCareer).where(CoachCareer.coach_id == item.id)
        )
        if career_rows:
            await session.execute(
                insert(CoachCareer).values(list(career_rows.values()))
            )
        stored += 1
    return stored


async def persist_transfers(
    session: AsyncSession, items: Sequence[TransferBundle]
) -> int:
    stored = 0
    for bundle in items:
        player = bundle.player
        player_id: int | None = None
        if isinstance(player, PersonCore):
            player_id = player.id
            await upsert_player_profiles(session, [player])
        elif isinstance(player, IdName) and player.id is not None:
            player_id = player.id
            await upsert_player_stubs(session, [(player.id, player.name, None)])
        if player_id is None:
            continue
        teams: list[IdName] = []
        rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for move in bundle.transfers:
            inn = move.teams.inn if move.teams is not None else None
            out = move.teams.out if move.teams is not None else None
            if inn is not None:
                teams.append(inn)
            if out is not None:
                teams.append(out)
            key = (
                player_id,
                move.date,
                out.id if out is not None else None,
                inn.id if inn is not None else None,
                move.type or "",
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "player_id": player_id,
                    "from_team_id": out.id if out is not None else None,
                    "to_team_id": inn.id if inn is not None else None,
                    "date": move.date,
                    "type": as_str(move.type, 50),
                    "api_update": bundle.update,
                }
            )
        await upsert_team_ids(session, teams)
        await session.execute(delete(Transfer).where(Transfer.player_id == player_id))
        if rows:
            await session.execute(insert(Transfer).values(rows))
            stored += len(rows)
    return stored


async def persist_trophies(
    session: AsyncSession,
    items: Sequence[TrophyItem],
    *,
    player_id: int | None = None,
    coach_id: int | None = None,
) -> int:
    if (player_id is None) == (coach_id is None):
        return 0
    if player_id is not None:
        await session.execute(delete(Trophy).where(Trophy.player_id == player_id))
    else:
        await session.execute(delete(Trophy).where(Trophy.coach_id == coach_id))
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "player_id": player_id,
                "coach_id": coach_id,
                "league_name": as_str(item.league, 255),
                "country": as_str(item.country, 100),
                "season": as_str(item.season, 32),
                "place": as_str(item.place, 100),
            }
        )
    if not rows:
        return 0
    await session.execute(insert(Trophy).values(rows))
    return len(rows)


async def persist_sidelined(
    session: AsyncSession,
    items: Sequence[SidelinedItem],
    *,
    player_id: int | None = None,
    coach_id: int | None = None,
) -> int:
    if (player_id is None) == (coach_id is None):
        return 0
    if player_id is not None:
        await session.execute(delete(Sidelined).where(Sidelined.player_id == player_id))
    else:
        await session.execute(delete(Sidelined).where(Sidelined.coach_id == coach_id))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for item in items:
        kind = as_str(item.type, 100)
        start = parse_flexible_date(item.start)
        if kind is None or start is None:
            continue
        key = (kind, start)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "player_id": player_id,
                "coach_id": coach_id,
                "type": kind,
                "start_date": start,
                "end_date": parse_flexible_date(item.end),
            }
        )
    if not rows:
        return 0
    await session.execute(insert(Sidelined).values(rows))
    return len(rows)
