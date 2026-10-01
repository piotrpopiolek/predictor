"""How close today's XI is to the side this team usually starts.

A regular is someone the coach keeps naming in the starting eleven. Rating,
goals and minutes do not change that: a starter taken off at 60 minutes is
still a starter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fractions import Fraction
from typing import Any, TypedDict

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import FINISHED_FIXTURE_STATUSES
from predictor.models.catalog import League, Player
from predictor.models.children import FixtureLineupPlayer
from predictor.models.fixtures import Fixture
from predictor.models.injuries import Injury
from predictor.models.seasonal import PlayerStatistic

WINDOW = 10
MIN_SAMPLE = 5
XI_SIZE = 11
LOOKBACK_DAYS = 180
SEASON_MIN_DENOM = 5

LABEL_STRONGEST = "Najmocniejszy skład"
LABEL_MIXED = "Mieszany skład"
LABEL_RESERVES = "Rezerwy"
LABEL_INJURED = "Osłabiony kontuzjami"
LABEL_THIN = "Za mało danych"

REASON_INJURY = "kontuzja"
REASON_BENCH = "na ławce"
REASON_OUT = "poza kadrą"

_CORE_SHARE = (3, 5)  # 0.60
_ROTATION_SHARE = (1, 4)  # 0.25
_BENCH_SHARE = (2, 5)  # 0.40
_CORE_MIN_STARTS = 3
_STRONGEST_SCORE = Fraction(3, 4)  # 0.75
_MIXED_SCORE = Fraction(9, 20)  # 0.45


class PlayerRole(StrEnum):
    CORE = "core"
    ROTATION = "rotation"
    RESERVE = "reserve"
    UNKNOWN = "unknown"


_WEIGHT = {
    PlayerRole.CORE: Fraction(1),
    PlayerRole.ROTATION: Fraction(1, 2),
    PlayerRole.RESERVE: Fraction(3, 20),
    PlayerRole.UNKNOWN: Fraction(9, 20),
}


@dataclass(frozen=True, slots=True)
class HistoryMatch:
    fixture_id: int
    kickoff: datetime | None
    season: int
    league_type: str | None
    players: tuple[tuple[int, bool], ...]


@dataclass(frozen=True, slots=True)
class SeasonStat:
    player_id: int
    lineups: int
    sub_bench: int
    league_id: int


@dataclass(frozen=True, slots=True)
class SquadPlayer:
    player_id: int
    is_starter: bool


@dataclass(frozen=True, slots=True)
class LineupStrength:
    label: str | None
    note: str | None
    missing: tuple[str, ...] = ()


class _Bucket(TypedDict):
    kickoff: datetime | None
    season: int
    league_type: str | None
    players: list[tuple[int, bool]]


def select_season_stats(
    rows: Sequence[SeasonStat],
    *,
    prefer_league_id: int | None,
) -> tuple[SeasonStat, ...]:
    """Keep one league: this competition when it is a league, else the busiest."""
    if prefer_league_id is not None:
        return tuple(row for row in rows if row.league_id == prefer_league_id)
    if not rows:
        return ()
    totals: dict[int, int] = {}
    for row in rows:
        totals[row.league_id] = totals.get(row.league_id, 0) + row.lineups
    chosen = max(totals, key=lambda league_id: (totals[league_id], -league_id))
    return tuple(row for row in rows if row.league_id == chosen)


def assess_lineup(
    history: Sequence[HistoryMatch],
    season_stats: Sequence[SeasonStat],
    today: Sequence[SquadPlayer],
    injured_ids: set[int],
    names: Mapping[int, str],
    *,
    season: int,
    kickoff: datetime | None,
) -> LineupStrength:
    """Label today's XI, or say the sample is too small to judge."""
    starters = tuple(player for player in today if player.is_starter)
    if len(starters) < XI_SIZE:
        return LineupStrength(None, None)
    window = _reference_window(history, season=season, kickoff=kickoff)
    if window is None:
        roles = _roles_from_season(season_stats)
        if not roles:
            return LineupStrength(LABEL_THIN, None)
        note = "ze statystyk sezonu"
    else:
        matches, source = window
        roles = _roles_from_window(matches)
        note = _sample_note(source, len(matches))
    label = _band(_mean_weight(starters, roles))
    missing_rows = _missing(roles, starters, today, injured_ids, names)
    if label == LABEL_MIXED and missing_rows:
        if all(reason == REASON_INJURY for _name, reason in missing_rows):
            label = LABEL_INJURED
    missing = tuple(f"{name} ({reason})" for name, reason in missing_rows)
    return LineupStrength(label, note, missing)


def _role(starts: int, bench: int, matches: int) -> PlayerRole:
    if _share_at_least(starts, matches, *_CORE_SHARE) and starts >= _CORE_MIN_STARTS:
        return PlayerRole.CORE
    frequent_bench = starts >= 1 and _share_at_least(bench, matches, *_BENCH_SHARE)
    if _share_at_least(starts, matches, *_ROTATION_SHARE) or frequent_bench:
        return PlayerRole.ROTATION
    if starts + bench >= 1:
        return PlayerRole.RESERVE
    return PlayerRole.UNKNOWN


def _share_at_least(count: int, total: int, numerator: int, denominator: int) -> bool:
    if total <= 0:
        return False
    return count * denominator >= total * numerator


def _reference_window(
    history: Sequence[HistoryMatch],
    *,
    season: int,
    kickoff: datetime | None,
) -> tuple[tuple[HistoryMatch, ...], str] | None:
    league = _recent(
        match
        for match in history
        if match.season == season
        and (match.league_type or "").strip().lower() == "league"
        and _full_xi(match)
        and _is_before(match, kickoff)
    )
    if len(league) >= MIN_SAMPLE:
        return league, "league"
    anchor = kickoff or datetime.now(UTC)
    cutoff = anchor - timedelta(days=LOOKBACK_DAYS)
    broader = _recent(
        match
        for match in history
        if _full_xi(match)
        and _is_before(match, kickoff)
        and match.kickoff is not None
        and match.kickoff >= cutoff
    )
    if len(broader) >= MIN_SAMPLE:
        return broader, "any"
    return None


def _recent(matches: Iterable[HistoryMatch]) -> tuple[HistoryMatch, ...]:
    ordered = sorted(matches, key=_recency, reverse=True)
    return tuple(ordered[:WINDOW])


def _recency(match: HistoryMatch) -> tuple[int, datetime, int]:
    if match.kickoff is None:
        return (0, datetime.min.replace(tzinfo=UTC), match.fixture_id)
    return (1, match.kickoff, match.fixture_id)


def _full_xi(match: HistoryMatch) -> bool:
    return sum(1 for _player_id, starter in match.players if starter) >= XI_SIZE


def _is_before(match: HistoryMatch, kickoff: datetime | None) -> bool:
    if kickoff is None:
        return True
    if match.kickoff is None:
        return False
    return match.kickoff < kickoff


def _roles_from_window(matches: Sequence[HistoryMatch]) -> dict[int, PlayerRole]:
    starts: dict[int, int] = {}
    bench: dict[int, int] = {}
    for match in matches:
        for player_id, is_starter in match.players:
            if is_starter:
                starts[player_id] = starts.get(player_id, 0) + 1
            else:
                bench[player_id] = bench.get(player_id, 0) + 1
    total = len(matches)
    player_ids = set(starts) | set(bench)
    return {
        player_id: _role(starts.get(player_id, 0), bench.get(player_id, 0), total)
        for player_id in player_ids
    }


def _roles_from_season(rows: Sequence[SeasonStat]) -> dict[int, PlayerRole]:
    roles: dict[int, PlayerRole] = {}
    for row in rows:
        total = row.lineups + row.sub_bench
        if total < SEASON_MIN_DENOM:
            continue
        roles[row.player_id] = _role(row.lineups, row.sub_bench, total)
    return roles


def _mean_weight(
    starters: Sequence[SquadPlayer], roles: Mapping[int, PlayerRole]
) -> Fraction:
    total = sum(
        (
            _WEIGHT[roles.get(player.player_id, PlayerRole.UNKNOWN)]
            for player in starters
        ),
        start=Fraction(0),
    )
    return total / len(starters)


def _band(score: Fraction) -> str:
    if score >= _STRONGEST_SCORE:
        return LABEL_STRONGEST
    if score >= _MIXED_SCORE:
        return LABEL_MIXED
    return LABEL_RESERVES


def _missing(
    roles: Mapping[int, PlayerRole],
    starters: Sequence[SquadPlayer],
    today: Sequence[SquadPlayer],
    injured_ids: set[int],
    names: Mapping[int, str],
) -> tuple[tuple[str, str], ...]:
    starter_ids = {player.player_id for player in starters}
    bench_ids = {player.player_id for player in today if not player.is_starter}
    named: list[tuple[str, str]] = []
    for player_id, role in roles.items():
        if role is not PlayerRole.CORE or player_id in starter_ids:
            continue
        if player_id in injured_ids:
            reason = REASON_INJURY
        elif player_id in bench_ids:
            reason = REASON_BENCH
        else:
            reason = REASON_OUT
        name = names.get(player_id, "").strip() or str(player_id)
        named.append((name, reason))
    named.sort(key=lambda item: (item[0].casefold(), item[1]))
    return tuple(named)


def _sample_note(source: str, count: int) -> str:
    if source == "league":
        return f"z ostatnich {count} meczów ligowych"
    return f"z ostatnich {count} meczów"


async def load_lineup_strength(
    session: AsyncSession,
    *,
    team_id: int,
    fixture_id: int,
    league_id: int,
    season: int,
    kickoff: datetime | None,
) -> LineupStrength:
    history = await _load_history(
        session,
        team_id=team_id,
        fixture_id=fixture_id,
        season=season,
        kickoff=kickoff,
    )
    current_type = await session.scalar(
        select(League.type).where(League.id == league_id)
    )
    prefer = league_id if (current_type or "").strip().lower() == "league" else None
    season_stats = select_season_stats(
        await _load_season_stats(session, team_id=team_id, season=season),
        prefer_league_id=prefer,
    )
    today = await _load_today(session, fixture_id=fixture_id, team_id=team_id)
    injured = await _load_injured(session, fixture_id=fixture_id, team_id=team_id)
    ids = {player.player_id for player in today}
    for match in history:
        ids.update(player_id for player_id, _starter in match.players)
    ids.update(row.player_id for row in season_stats)
    return assess_lineup(
        history,
        season_stats,
        today,
        injured,
        await _load_names(session, ids),
        season=season,
        kickoff=kickoff,
    )


async def _load_history(
    session: AsyncSession,
    *,
    team_id: int,
    fixture_id: int,
    season: int,
    kickoff: datetime | None,
) -> tuple[HistoryMatch, ...]:
    anchor = kickoff or datetime.now(UTC)
    cutoff = anchor - timedelta(days=LOOKBACK_DAYS)
    rows = (
        await session.execute(
            select(
                Fixture.id,
                Fixture.date,
                Fixture.season,
                League.type,
                FixtureLineupPlayer.player_id,
                FixtureLineupPlayer.is_starter,
            )
            .join(League, League.id == Fixture.league_id)
            .join(
                FixtureLineupPlayer,
                and_(
                    FixtureLineupPlayer.fixture_id == Fixture.id,
                    FixtureLineupPlayer.team_id == team_id,
                ),
            )
            .where(
                or_(Fixture.home_team_id == team_id, Fixture.away_team_id == team_id),
                Fixture.id != fixture_id,
                Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)),
                or_(
                    and_(
                        Fixture.season == season,
                        func.lower(League.type) == "league",
                    ),
                    Fixture.date >= cutoff,
                ),
            )
        )
    ).all()
    return _group_history(rows)


def _group_history(rows: Sequence[Any]) -> tuple[HistoryMatch, ...]:
    grouped: dict[int, _Bucket] = {}
    order: list[int] = []
    for fixture_id, kickoff, season, league_type, player_id, is_starter in rows:
        fid = int(fixture_id)
        slot = grouped.get(fid)
        if slot is None:
            slot = _Bucket(
                kickoff=kickoff if isinstance(kickoff, datetime) else None,
                season=int(season),
                league_type=None if league_type is None else str(league_type),
                players=[],
            )
            grouped[fid] = slot
            order.append(fid)
        slot["players"].append((int(player_id), bool(is_starter)))
    return tuple(
        HistoryMatch(
            fixture_id=fid,
            kickoff=grouped[fid]["kickoff"],
            season=grouped[fid]["season"],
            league_type=grouped[fid]["league_type"],
            players=tuple(grouped[fid]["players"]),
        )
        for fid in order
    )


async def _load_season_stats(
    session: AsyncSession, *, team_id: int, season: int
) -> tuple[SeasonStat, ...]:
    rows = (
        await session.execute(
            select(
                PlayerStatistic.league_id,
                PlayerStatistic.player_id,
                PlayerStatistic.lineups,
                PlayerStatistic.sub_bench,
            )
            .join(League, League.id == PlayerStatistic.league_id)
            .where(
                PlayerStatistic.team_id == team_id,
                PlayerStatistic.season == season,
                func.lower(League.type) == "league",
            )
        )
    ).all()
    return tuple(
        SeasonStat(
            player_id=int(player_id),
            lineups=int(lineups or 0),
            sub_bench=int(sub_bench or 0),
            league_id=int(league_id),
        )
        for league_id, player_id, lineups, sub_bench in rows
    )


async def _load_today(
    session: AsyncSession, *, fixture_id: int, team_id: int
) -> tuple[SquadPlayer, ...]:
    rows = (
        await session.execute(
            select(
                FixtureLineupPlayer.player_id,
                FixtureLineupPlayer.is_starter,
            ).where(
                FixtureLineupPlayer.fixture_id == fixture_id,
                FixtureLineupPlayer.team_id == team_id,
            )
        )
    ).all()
    return tuple(
        SquadPlayer(player_id=int(player_id), is_starter=bool(starter))
        for player_id, starter in rows
    )


async def _load_injured(
    session: AsyncSession, *, fixture_id: int, team_id: int
) -> set[int]:
    rows = (
        await session.execute(
            select(Injury.player_id).where(
                Injury.fixture_id == fixture_id,
                Injury.team_id == team_id,
            )
        )
    ).all()
    return {int(player_id) for (player_id,) in rows}


async def _load_names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(select(Player.id, Player.name).where(Player.id.in_(ids)))
    ).all()
    found: dict[int, str] = {}
    for player_id, name in rows:
        text = "" if name is None else str(name).strip()
        if text:
            found[int(player_id)] = text
    return found
