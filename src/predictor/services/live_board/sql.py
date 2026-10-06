"""Async live_board loaders (read-only DB)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    func,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
)
from sqlalchemy.orm import aliased

from predictor.constants import FINISHED_FIXTURE_STATUSES
from predictor.models.catalog import (
    Bookmaker,
    League,
    OddsBet,
    OddsLiveBet,
    Player,
)
from predictor.models.children import (
    FixtureEvent,
    FixtureLineup,
    FixtureStatistic,
)
from predictor.models.etl import EtlTask
from predictor.models.fixtures import (
    Fixture,
    Team,
    Venue,
)
from predictor.models.odds import (
    FixtureOdds,
    FixtureOddsLive,
)
from predictor.models.predictions import Prediction
from predictor.models.seasonal import TeamSeasonStatistics
from predictor.services.ingest.persist_live import mapped_next_goal_ids
from predictor.services.live_board.domain import (
    _KEY_STATS,
    _PREMATCH_MARKETS,
    _STAT_CORNERS,
    _STAT_POSSESSION,
    _STAT_REDS,
    _STAT_SHOTS,
    _STAT_SHOTS_ON,
    FORM_LAST_MATCHES,
    FirstLegScore,
    LiveMatch,
    LiveScorer,
    LiveStats,
    TeamGoalForm,
    _blank_to_none,
    _name,
    avg_minute_from_goal_bins,
    event_kind,
    goal_clock_minute,
    is_fulltime_1x2_market,
    is_second_leg,
    order_day_matches,
    parse_live_fixture_ids,
    pick_venue,
    safe_http_url,
    scoring_team_id,
    select_live_1x2,
    select_next_goal_matches,
    select_next_goal_odds,
    select_prematch_odds,
    sort_matches_by_clock,
    summarize_team_goal_form,
)


async def load_live_all_fixture_ids(session: AsyncSession) -> list[int]:
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains({"live": "all"}))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        return []
    return parse_live_fixture_ids(task.params)

def _board_stmt() -> Any:
    home = aliased(Team)
    away = aliased(Team)
    return (
        select(
            Fixture.id,
            League.country_name,
            League.name,
            Fixture.round,
            home.name,
            away.name,
            home.logo,
            away.logo,
            Fixture.goals_home,
            Fixture.goals_away,
            Fixture.status_short,
            Fixture.status_long,
            Fixture.elapsed_minutes,
            Fixture.extra_minutes,
            Fixture.home_team_id,
            Fixture.away_team_id,
            League.logo,
            Fixture.venue_id,
            Fixture.venue_name,
            Fixture.venue_city,
            Venue.name,
            Venue.city,
            Fixture.referee,
            Fixture.goals_home_halftime,
            Fixture.goals_away_halftime,
            Fixture.goals_home_extratime,
            Fixture.goals_away_extratime,
            Fixture.goals_home_penalty,
            Fixture.goals_away_penalty,
            Fixture.league_id,
            Fixture.season,
            Fixture.date,
            Fixture.leg,
        )
        .join(League, League.id == Fixture.league_id)
        .join(home, home.id == Fixture.home_team_id)
        .join(away, away.id == Fixture.away_team_id)
        .outerjoin(Venue, Venue.id == Fixture.venue_id)
    )

def _assemble_matches(
    rows: Sequence[Any], extras: dict[int, dict[str, Any]]
) -> list[LiveMatch]:
    matches: list[LiveMatch] = []
    for row in rows:
        fixture_id = int(row[0])
        extra = extras.get(fixture_id, _empty_extras())
        venue, venue_city = pick_venue(row[17], row[18], row[19], row[20], row[21])
        status = str(row[10] or "LIVE")
        matches.append(
            LiveMatch(
                fixture_id=fixture_id,
                country=_name(row[1]),
                league=_name(row[2]),
                round=row[3],
                home=_name(row[4]),
                away=_name(row[5]),
                home_logo=safe_http_url(row[6]),
                away_logo=safe_http_url(row[7]),
                goals_home=row[8],
                goals_away=row[9],
                status_short=status,
                status_long=row[11],
                elapsed=row[12],
                extra=row[13],
                home_team_id=int(row[14]),
                away_team_id=int(row[15]),
                league_logo=safe_http_url(row[16]),
                venue=venue,
                venue_city=venue_city,
                referee=_blank_to_none(row[22]),
                ht_home=row[23],
                ht_away=row[24],
                et_home=row[25],
                et_away=row[26],
                pen_home=row[27],
                pen_away=row[28],
                league_id=int(row[29]),
                season=int(row[30]),
                kickoff=row[31],
                leg=row[32],
                home_reds=extra["home_reds"],
                away_reds=extra["away_reds"],
                home_formation=extra["home_formation"],
                away_formation=extra["away_formation"],
                scorers=extra["scorers"],
                stats=extra["stats"],
                prematch=extra["prematch"],
                next_goal=extra["next_goal"],
                form_home=extra["form_home"],
                form_away=extra["form_away"],
                prediction_winner_team_id=extra["prediction_winner_team_id"],
                prediction_pct_home=extra["prediction_pct_home"],
                prediction_pct_away=extra["prediction_pct_away"],
            )
        )
    return matches

async def list_day_matches(engine: AsyncEngine, day: date) -> list[LiveMatch]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            _board_stmt()
            .where(Fixture.date.is_not(None))
            .where(Fixture.date >= start)
            .where(Fixture.date < end)
            .order_by(Fixture.date.asc(), Fixture.id)
        )
        rows = (await session.execute(stmt)).all()
        extras = await _load_extras(session, rows)
    return order_day_matches(_assemble_matches(rows, extras))

async def list_live_matches(engine: AsyncEngine) -> list[LiveMatch]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        live_ids = await load_live_all_fixture_ids(session)
        if not live_ids:
            return []
        stmt = (
            _board_stmt()
            .where(Fixture.id.in_(live_ids))
            .order_by(
                League.country_name.asc().nulls_last(),
                League.name.asc().nulls_last(),
                Fixture.elapsed_minutes.desc().nulls_last(),
                Fixture.id,
            )
        )
        rows = (await session.execute(stmt)).all()
        extras = await _load_extras(session, rows)
    return sort_matches_by_clock(_assemble_matches(rows, extras))

async def load_fixture_match(engine: AsyncEngine, fixture_id: int) -> LiveMatch | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(_board_stmt().where(Fixture.id == fixture_id))
        ).all()
        if not rows:
            return None
        extras = await _load_extras(session, rows)
    assembled = _assemble_matches(rows, extras)
    return assembled[0] if assembled else None

async def load_first_legs(
    engine: AsyncEngine, matches: Sequence[LiveMatch]
) -> dict[int, FirstLegScore]:
    candidates = [
        match
        for match in matches
        if is_second_leg(match.round, match.leg)
        and match.home_team_id
        and match.away_team_id
        and match.league_id
    ]
    if not candidates:
        return {}
    live_ids = [match.fixture_id for match in candidates]
    team_pairs = {
        (min(m.home_team_id, m.away_team_id), max(m.home_team_id, m.away_team_id))
        for m in candidates
    }
    league_ids = sorted({m.league_id for m in candidates})
    pair_filter = tuple_(
        func.least(Fixture.home_team_id, Fixture.away_team_id),
        func.greatest(Fixture.home_team_id, Fixture.away_team_id),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            select(
                Fixture.id,
                Fixture.league_id,
                Fixture.home_team_id,
                Fixture.away_team_id,
                Fixture.goals_home,
                Fixture.goals_away,
                Fixture.goals_home_fulltime,
                Fixture.goals_away_fulltime,
                Fixture.date,
            )
            .where(Fixture.league_id.in_(league_ids))
            .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
            .where(Fixture.id.not_in(live_ids))
            .where(pair_filter.in_(list(team_pairs)))
            .order_by(Fixture.date.desc().nulls_last(), Fixture.id.desc())
        )
        rows = (await session.execute(stmt)).all()

    def _score(row: Any) -> FirstLegScore | None:
        goals_home = row[6] if row[6] is not None else row[4]
        goals_away = row[7] if row[7] is not None else row[5]
        if goals_home is None or goals_away is None:
            return None
        return FirstLegScore(
            home_team_id=int(row[2]),
            away_team_id=int(row[3]),
            goals_home=int(goals_home),
            goals_away=int(goals_away),
        )

    result: dict[int, FirstLegScore] = {}
    for match in candidates:
        for row in rows:
            if int(row[1]) != match.league_id:
                continue
            if {int(row[2]), int(row[3])} != {
                match.home_team_id,
                match.away_team_id,
            }:
                continue
            kickoff = row[8]
            if (
                match.kickoff is not None
                and kickoff is not None
                and kickoff >= match.kickoff
            ):
                continue
            scored = _score(row)
            if scored is None:
                continue
            result[match.fixture_id] = scored
            break
    return result

async def list_next_goal_matches(
    engine: AsyncEngine,
) -> list[tuple[LiveMatch, tuple[str, ...]]]:
    matches = await list_live_matches(engine)
    first_legs = await load_first_legs(engine, matches)
    return select_next_goal_matches(matches, first_legs)

def _empty_extras() -> dict[str, Any]:
    return {
        "home_reds": 0,
        "away_reds": 0,
        "home_formation": None,
        "away_formation": None,
        "scorers": (),
        "stats": None,
        "prematch": None,
        "next_goal": None,
        "form_home": None,
        "form_away": None,
        "prediction_winner_team_id": None,
        "prediction_pct_home": None,
        "prediction_pct_away": None,
    }

async def _load_extras(
    session: AsyncSession, rows: Sequence[Any]
) -> dict[int, dict[str, Any]]:
    if not rows:
        return {}
    ids = [int(row[0]) for row in rows]
    teams = {int(row[0]): (int(row[14]), int(row[15])) for row in rows}
    team_context: dict[int, tuple[int, int]] = {}
    for row in rows:
        league_id = int(row[29])
        season = int(row[30])
        for team_id in (int(row[14]), int(row[15])):
            team_context[team_id] = (league_id, season)
    extras = {fid: _empty_extras() for fid in ids}
    await _fill_events(session, ids, teams, extras)
    await _fill_stats(session, ids, teams, extras)
    await _fill_lineups(session, ids, extras)
    await _fill_prematch(session, ids, extras)
    await _fill_predictions(session, ids, extras)
    await _fill_next_goal(session, rows, extras)
    await _fill_goal_form(session, ids, teams, extras, team_context)
    return extras

async def _fill_events(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureEvent.fixture_id,
            FixtureEvent.team_id,
            FixtureEvent.event_type,
            FixtureEvent.detail,
            FixtureEvent.minute,
            FixtureEvent.minute_extra,
            FixtureEvent.sort_order,
            Player.name,
        )
        .outerjoin(Player, Player.id == FixtureEvent.player_id)
        .where(FixtureEvent.fixture_id.in_(ids))
        .where(FixtureEvent.event_type.in_(("Goal", "Card")))
        .order_by(
            FixtureEvent.fixture_id,
            FixtureEvent.sort_order.asc().nulls_last(),
            FixtureEvent.minute,
            FixtureEvent.id,
        )
    )
    for row in (await session.execute(stmt)).all():
        fixture_id = int(row[0])
        home_id, away_id = teams[fixture_id]
        kind = event_kind(str(row[2]), row[3])
        if kind is None:
            continue
        side = _team_side(int(row[1]), home_id, away_id)
        if side is None:
            continue
        if kind == "red":
            extras[fixture_id][f"{side}_reds"] += 1
            continue
        extras[fixture_id]["scorers"] = extras[fixture_id]["scorers"] + (
            LiveScorer(
                side=side,
                name=_name(row[7]),
                minute=int(row[4]),
                extra=row[5],
                kind=kind,
            ),
        )

def _team_side(team_id: int, home_id: int, away_id: int) -> str | None:
    if team_id == home_id:
        return "home"
    if team_id == away_id:
        return "away"
    return None

def _side_appearances(
    team_ids: list[int],
    live_ids: list[int],
    team_col: Any,
    goals_col: Any,
) -> Any:
    rn = func.row_number().over(
        partition_by=team_col,
        order_by=(Fixture.date.desc().nulls_last(), Fixture.id.desc()),
    )
    inner = (
        select(
            Fixture.id.label("fixture_id"),
            team_col.label("team_id"),
            goals_col.label("goals_for"),
            Fixture.home_team_id.label("home_team_id"),
            Fixture.away_team_id.label("away_team_id"),
            Fixture.date.label("kickoff"),
            rn.label("rn"),
        )
        .where(team_col.in_(team_ids))
        .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
    )
    if live_ids:
        inner = inner.where(Fixture.id.not_in(live_ids))
    sub = inner.subquery()
    return select(
        sub.c.fixture_id,
        sub.c.team_id,
        sub.c.goals_for,
        sub.c.home_team_id,
        sub.c.away_team_id,
        sub.c.kickoff,
    ).where(sub.c.rn <= FORM_LAST_MATCHES)

def _appearance_sort_key(row: Any) -> tuple[int, float, int]:
    kickoff = row.kickoff
    if kickoff is None:
        return (1, 0.0, -int(row.fixture_id))
    return (0, -kickoff.timestamp(), -int(row.fixture_id))

async def _fill_goal_form(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
    team_context: dict[int, tuple[int, int]] | None = None,
) -> None:
    team_ids = sorted({tid for pair in teams.values() for tid in pair})
    if not team_ids:
        return
    stmt = union_all(
        _side_appearances(team_ids, ids, Fixture.home_team_id, Fixture.goals_home),
        _side_appearances(team_ids, ids, Fixture.away_team_id, Fixture.goals_away),
    )
    appearances: dict[int, list[Any]] = defaultdict(list)
    fixture_sides: dict[int, tuple[int, int]] = {}
    for row in (await session.execute(stmt)).all():
        appearances[int(row.team_id)].append(row)
        fixture_sides[int(row.fixture_id)] = (
            int(row.home_team_id),
            int(row.away_team_id),
        )
    for rows in appearances.values():
        rows.sort(key=_appearance_sort_key)
        del rows[FORM_LAST_MATCHES:]
    wanted_by_team = {
        team_id: {int(row.fixture_id) for row in rows}
        for team_id, rows in appearances.items()
    }
    form_ids = sorted({fid for fids in wanted_by_team.values() for fid in fids})
    minutes_by_team: dict[int, list[int]] = defaultdict(list)
    if form_ids:
        events = (
            select(
                FixtureEvent.fixture_id,
                FixtureEvent.team_id,
                FixtureEvent.detail,
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
            )
            .where(FixtureEvent.fixture_id.in_(form_ids))
            .where(FixtureEvent.event_type == "Goal")
        )
        for row in (await session.execute(events)).all():
            fixture_id = int(row.fixture_id)
            sides = fixture_sides.get(fixture_id)
            if sides is None:
                continue
            kind = event_kind("Goal", row.detail)
            scorer = scoring_team_id(int(row.team_id), sides[0], sides[1], kind)
            if scorer is None or fixture_id not in wanted_by_team.get(scorer, set()):
                continue
            minutes_by_team[scorer].append(
                goal_clock_minute(int(row.minute), row.minute_extra)
            )
    forms = {
        team_id: summarize_team_goal_form(
            [row.goals_for for row in rows],
            minutes_by_team.get(team_id, ()),
        )
        for team_id, rows in appearances.items()
    }
    await _apply_season_goal_minute_fallback(session, forms, team_context or {})
    for fixture_id, (home_id, away_id) in teams.items():
        extras[fixture_id]["form_home"] = forms.get(home_id)
        extras[fixture_id]["form_away"] = forms.get(away_id)

async def _apply_season_goal_minute_fallback(
    session: AsyncSession,
    forms: dict[int, TeamGoalForm | None],
    team_context: dict[int, tuple[int, int]],
) -> None:
    """When last-15 events lack Goal minutes, use season gf_minute bins."""
    missing = [
        team_id
        for team_id, form in forms.items()
        if form is not None and form.avg_minute is None and team_id in team_context
    ]
    if not missing:
        return
    keys = {(team_id, *team_context[team_id]) for team_id in missing}
    league_ids = sorted({league for _, league, _ in keys})
    seasons = sorted({season for _, _, season in keys})
    rows = (
        await session.execute(
            select(
                TeamSeasonStatistics.team_id,
                TeamSeasonStatistics.league_id,
                TeamSeasonStatistics.season,
                TeamSeasonStatistics.as_of_date,
                TeamSeasonStatistics.gf_minute,
            )
            .where(TeamSeasonStatistics.team_id.in_(missing))
            .where(TeamSeasonStatistics.league_id.in_(league_ids))
            .where(TeamSeasonStatistics.season.in_(seasons))
            .order_by(TeamSeasonStatistics.as_of_date.desc())
        )
    ).all()
    bins_by_team: dict[int, object] = {}
    for row in rows:
        team_id = int(row.team_id)
        if team_id in bins_by_team:
            continue
        expected = team_context.get(team_id)
        if expected is None:
            continue
        if (int(row.league_id), int(row.season)) != expected:
            continue
        bins_by_team[team_id] = row.gf_minute
    for team_id, form in list(forms.items()):
        if form is None or form.avg_minute is not None:
            continue
        avg_minute = avg_minute_from_goal_bins(bins_by_team.get(team_id))
        if avg_minute is None:
            continue
        forms[team_id] = TeamGoalForm(
            matches=form.matches,
            avg_goals=form.avg_goals,
            avg_minute=avg_minute,
        )

async def _fill_stats(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureStatistic.fixture_id,
            FixtureStatistic.team_id,
            FixtureStatistic.stat_type,
            FixtureStatistic.stat_value,
        )
        .where(FixtureStatistic.fixture_id.in_(ids))
        .where(FixtureStatistic.stat_type.in_(_KEY_STATS))
        .where(FixtureStatistic.period == "FT")
    )
    buckets: dict[int, dict[str, dict[str, str | None]]] = {}
    for row in (await session.execute(stmt)).all():
        fixture_id = int(row[0])
        home_id, away_id = teams[fixture_id]
        side = _team_side(int(row[1]), home_id, away_id)
        if side is None:
            continue
        buckets.setdefault(fixture_id, {}).setdefault(str(row[2]), {})[side] = (
            _blank_to_none(row[3])
        )
    for fixture_id, stats in buckets.items():
        reds = stats.get(_STAT_REDS, {})
        if extras[fixture_id]["home_reds"] == 0:
            extras[fixture_id]["home_reds"] = _stat_count(reds.get("home"))
        if extras[fixture_id]["away_reds"] == 0:
            extras[fixture_id]["away_reds"] = _stat_count(reds.get("away"))
        live_stats = LiveStats(
            possession_home=_stat_pair(stats, _STAT_POSSESSION, "home"),
            possession_away=_stat_pair(stats, _STAT_POSSESSION, "away"),
            shots_on_home=_stat_pair(stats, _STAT_SHOTS_ON, "home"),
            shots_on_away=_stat_pair(stats, _STAT_SHOTS_ON, "away"),
            shots_home=_stat_pair(stats, _STAT_SHOTS, "home"),
            shots_away=_stat_pair(stats, _STAT_SHOTS, "away"),
            corners_home=_stat_pair(stats, _STAT_CORNERS, "home"),
            corners_away=_stat_pair(stats, _STAT_CORNERS, "away"),
        )
        extras[fixture_id]["stats"] = None if live_stats.is_empty() else live_stats

def _stat_pair(
    stats: dict[str, dict[str, str | None]], key: str, side: str
) -> str | None:
    return stats.get(key, {}).get(side)

def _stat_count(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 0

async def _fill_lineups(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = select(
        FixtureLineup.fixture_id,
        FixtureLineup.is_home,
        FixtureLineup.formation,
    ).where(FixtureLineup.fixture_id.in_(ids))
    for fixture_id, is_home, formation in (await session.execute(stmt)).all():
        key = "home_formation" if is_home else "away_formation"
        extras[int(fixture_id)][key] = _blank_to_none(formation)

async def _fill_prematch(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureOdds.fixture_id,
            FixtureOdds.bookmaker_id,
            Bookmaker.name,
            OddsBet.name,
            FixtureOdds.value_label,
            FixtureOdds.odd,
        )
        .join(OddsBet, OddsBet.id == FixtureOdds.bet_id)
        .join(Bookmaker, Bookmaker.id == FixtureOdds.bookmaker_id)
        .where(FixtureOdds.fixture_id.in_(ids))
        .where(func.lower(OddsBet.name).in_(_PREMATCH_MARKETS))
    )
    by_fixture: dict[int, list[tuple[int, str | None, str | None, str, Decimal]]] = {}
    for fid, book_id, book_name, bet_name, label, odd in (
        await session.execute(stmt)
    ).all():
        by_fixture.setdefault(int(fid), []).append(
            (int(book_id), book_name, bet_name, str(label), Decimal(odd))
        )
    for fixture_id, odds_rows in by_fixture.items():
        extras[fixture_id]["prematch"] = select_prematch_odds(odds_rows)
    missing = [fid for fid in ids if extras[fid]["prematch"] is None]
    if missing:
        await _fill_live_opening_1x2(session, missing, extras)

async def _fill_live_opening_1x2(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    """Attach live 1X2. Prefer a snapshot at or before kickoff; otherwise the latest."""
    bet_rows = (await session.execute(select(OddsLiveBet.id, OddsLiveBet.name))).all()
    bet_ids = [
        int(bet_id)
        for bet_id, name in bet_rows
        if name and is_fulltime_1x2_market(name)
    ]
    if not bet_ids:
        return
    kickoffs = {
        int(fid): kickoff
        for fid, kickoff in (
            await session.execute(
                select(Fixture.id, Fixture.date).where(Fixture.id.in_(ids))
            )
        ).all()
    }
    earliest = (
        select(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at)
        .where(FixtureOddsLive.fixture_id.in_(ids))
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
        .distinct(FixtureOddsLive.fixture_id)
        .order_by(
            FixtureOddsLive.fixture_id,
            FixtureOddsLive.captured_at.asc(),
        )
    )
    pairs = []
    for fid, captured in (await session.execute(earliest)).all():
        if captured is None:
            continue
        kickoff = kickoffs.get(int(fid))
        if kickoff is not None and captured > kickoff:
            continue
        pairs.append((int(fid), captured))
    covered = {fid for fid, _captured in pairs}
    missing = [fid for fid in ids if fid not in covered]
    if missing:
        latest = (
            select(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at)
            .where(FixtureOddsLive.fixture_id.in_(missing))
            .where(FixtureOddsLive.bet_id.in_(bet_ids))
            .distinct(FixtureOddsLive.fixture_id)
            .order_by(
                FixtureOddsLive.fixture_id,
                FixtureOddsLive.captured_at.desc(),
            )
        )
        pairs.extend(
            (int(fid), captured)
            for fid, captured in (await session.execute(latest)).all()
            if captured is not None
        )
    if not pairs:
        return
    stmt = (
        select(
            FixtureOddsLive.fixture_id,
            OddsLiveBet.name,
            FixtureOddsLive.value_label,
            FixtureOddsLive.odd,
        )
        .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
        .where(
            tuple_(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at).in_(pairs)
        )
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
    )
    by_fixture: dict[int, list[tuple[str | None, str, Decimal]]] = {}
    for fid, name, label, odd in (await session.execute(stmt)).all():
        by_fixture.setdefault(int(fid), []).append((name, str(label), Decimal(odd)))
    for fixture_id, odds_rows in by_fixture.items():
        extras[fixture_id]["prematch"] = select_live_1x2(odds_rows)

def _parse_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

async def _fill_predictions(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = select(
        Prediction.fixture_id,
        Prediction.winner_team_id,
        Prediction.pct_home,
        Prediction.pct_away,
    ).where(Prediction.fixture_id.in_(ids))
    rows = (await session.execute(stmt)).all()
    for fixture_id, winner_id, pct_home, pct_away in rows:
        extras[int(fixture_id)]["prediction_winner_team_id"] = (
            None if winner_id is None else int(winner_id)
        )
        extras[int(fixture_id)]["prediction_pct_home"] = _parse_pct(pct_home)
        extras[int(fixture_id)]["prediction_pct_away"] = _parse_pct(pct_away)

async def _fill_next_goal(
    session: AsyncSession,
    rows: Sequence[Any],
    extras: dict[int, dict[str, Any]],
) -> None:
    ids = [int(row[0]) for row in rows]
    mapped = await _mapped_next_goal_ids(session)
    if not mapped:
        return
    latest = (
        select(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at)
        .where(FixtureOddsLive.fixture_id.in_(ids))
        .distinct(FixtureOddsLive.fixture_id)
        .order_by(
            FixtureOddsLive.fixture_id,
            FixtureOddsLive.captured_at.desc(),
        )
    )
    pairs = [
        (int(fid), captured)
        for fid, captured in (await session.execute(latest)).all()
        if captured is not None
    ]
    if not pairs:
        return
    stmt = (
        select(
            FixtureOddsLive.fixture_id,
            OddsLiveBet.name,
            FixtureOddsLive.value_label,
            FixtureOddsLive.odd,
            FixtureOddsLive.suspended,
        )
        .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
        .where(
            tuple_(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at).in_(pairs)
        )
        .where(FixtureOddsLive.bet_id.in_(mapped))
    )
    by_fixture: dict[int, list[tuple[str | None, str, Decimal, bool | None]]] = {}
    for fid, name, label, odd, suspended in (await session.execute(stmt)).all():
        by_fixture.setdefault(int(fid), []).append(
            (name, str(label), Decimal(odd), suspended)
        )
    meta = {
        int(row[0]): (str(row[10] or "LIVE"), row[8], row[9], row[25], row[26])
        for row in rows
    }
    for fixture_id, odds_rows in by_fixture.items():
        status, goals_home, goals_away, et_home, et_away = meta[fixture_id]
        extras[fixture_id]["next_goal"] = select_next_goal_odds(
            odds_rows, status, goals_home, goals_away, et_home, et_away
        )

async def _mapped_next_goal_ids(session: AsyncSession) -> set[int]:
    return await mapped_next_goal_ids(session)
