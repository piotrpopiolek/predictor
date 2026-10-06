"""Async match_detail loaders (read-only DB)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
)
from sqlalchemy.orm import aliased

from predictor.constants import FINISHED_FIXTURE_STATUSES
from predictor.models.catalog import (
    OddsLiveBet,
    Player,
)
from predictor.models.children import (
    FixtureEvent,
    FixtureLineupPlayer,
    FixtureStatistic,
)
from predictor.models.fixtures import (
    Fixture,
    Team,
)
from predictor.models.odds import FixtureOddsLive
from predictor.models.predictions import (
    Prediction,
    PredictionH2H,
)
from predictor.models.seasonal import Standing
from predictor.services.lineup_strength import (
    XI_SIZE,
    LineupStrength,
    load_lineup_strength,
)
from predictor.services.live_board import (
    FORM_LAST_MATCHES,
    LiveMatch,
    load_fixture_match,
)
from predictor.services.match_detail.domain import (
    _OVER_MARKETS,
    AttackChange,
    DetailEvent,
    DetailH2H,
    DetailPlayer,
    DetailStat,
    GoalPrice,
    GroupTable,
    MatchDetail,
    OddsMarket,
    StandingSlot,
    _handicap_num,
    _none_from_next_goal,
    _none_odd,
    _over_odd,
    _snapshot_includes_match,
    build_group_table,
    read_goal_price,
    recent_attack_changes,
    select_display_markets,
)

_STAT_ORDER = (
    "Ball Possession",
    "Shots on Goal",
    "Total Shots",
    "Shots off Goal",
    "Blocked Shots",
    "Shots insidebox",
    "Shots outsidebox",
    "Corner Kicks",
    "Offsides",
    "Fouls",
    "Yellow Cards",
    "Red Cards",
    "Goalkeeper Saves",
    "Total passes",
    "Passes accurate",
    "Passes %",
    "expected_goals",
)

_STAT_LABELS = {
    "Ball Possession": "Posiadanie",
    "Shots on Goal": "Strzały celne",
    "Total Shots": "Strzały",
    "Shots off Goal": "Strzały niecelne",
    "Blocked Shots": "Zablokowane",
    "Shots insidebox": "W polu karnym",
    "Shots outsidebox": "Spoza pola",
    "Corner Kicks": "Rzuty rożne",
    "Offsides": "Spalone",
    "Fouls": "Faule",
    "Yellow Cards": "Żółte kartki",
    "Red Cards": "Czerwone kartki",
    "Goalkeeper Saves": "Obrony",
    "Total passes": "Podania",
    "Passes accurate": "Podania celne",
    "Passes %": "Celność podań",
    "expected_goals": "xG",
}

_COMPARE_LABELS = {
    "form": "Forma",
    "att": "Atak",
    "def": "Obrona",
    "poisson_distribution": "Poisson",
    "h2h": "H2H",
    "goals": "Gole",
    "total": "Ogółem",
}


async def load_match_detail(engine: AsyncEngine, fixture_id: int) -> MatchDetail | None:
    match = await load_fixture_match(engine, fixture_id)
    if match is None:
        return None
    async with AsyncSession(engine, expire_on_commit=False) as session:
        events = await _events(session, match)
        stats = await _stats(session, match)
        home_xi, away_xi, home_bench, away_bench = await _lineup(session, match)
        home_strength = await _lineup_strength(session, match, len(home_xi), home=True)
        away_strength = await _lineup_strength(session, match, len(away_xi), home=False)
        prediction = await session.get(Prediction, fixture_id)
        h2h = await _h2h(session, fixture_id)
        form_results = await _recent_results(session, match)
        table = await _group_table(session, match)
        markets = await _live_markets(session, match)
        attack_subs = await _attack_subs(session, match)
        goal_price = await _goal_price(session, match)
    advice = None
    expected_home = None
    expected_away = None
    pct_draw = None
    comparison: tuple[tuple[str, str, str], ...] = ()
    if prediction is not None:
        advice = _blank(prediction.advice)
        expected_home = _blank(prediction.goals_home)
        expected_away = _blank(prediction.goals_away)
        pct_draw = _blank(prediction.pct_draw)
        comparison = _comparison_rows(prediction.comparison)
    return MatchDetail(
        match=match,
        events=tuple(events),
        stats=tuple(stats),
        home_xi=tuple(home_xi),
        away_xi=tuple(away_xi),
        home_bench=tuple(home_bench),
        away_bench=tuple(away_bench),
        home_strength=home_strength,
        away_strength=away_strength,
        advice=advice,
        expected_home=expected_home,
        expected_away=expected_away,
        pct_draw=pct_draw,
        comparison=comparison,
        h2h=tuple(h2h),
        table=table,
        markets=tuple(markets),
        attack_subs=tuple(attack_subs),
        goal_price=goal_price,
        form_results=form_results,
    )


async def _group_table(session: AsyncSession, match: LiveMatch) -> GroupTable | None:
    rows = (
        await session.execute(
            select(Standing, Team.name)
            .join(Team, Team.id == Standing.team_id)
            .where(Standing.league_id == match.league_id)
            .where(Standing.season == match.season)
        )
    ).all()
    if not rows:
        return None
    by_group: dict[str, list[StandingSlot]] = {}
    captured_at: datetime | None = None
    for standing, name in rows:
        by_group.setdefault(standing.group_name or "", []).append(
            StandingSlot(
                team_id=int(standing.team_id),
                name=_blank(name) or "—",
                points=int(standing.points or 0),
                played=int(standing.played or 0),
                gf=int(standing.goals_for or 0),
                ga=int(standing.goals_against or 0),
                api_rank=int(standing.rank),
            )
        )
        updated = standing.api_update
        if isinstance(updated, datetime) and (
            captured_at is None or updated > captured_at
        ):
            captured_at = updated
    chosen: tuple[str, list[StandingSlot]] | None = None
    for group_name, slots in by_group.items():
        ids = {slot.team_id for slot in slots}
        if match.home_team_id in ids and match.away_team_id in ids:
            chosen = (group_name, slots)
            break
    if chosen is None:
        return None
    group_name, slots = chosen
    played = {slot.team_id: slot.played for slot in slots}
    others = (
        await session.execute(
            select(Fixture.home_team_id, Fixture.away_team_id).where(
                Fixture.league_id == match.league_id,
                Fixture.season == match.season,
                Fixture.status_short.in_(FINISHED_FIXTURE_STATUSES),
                Fixture.id != match.fixture_id,
            )
        )
    ).all()
    finished: dict[int, int] = {}
    for home_id, away_id in others:
        finished[int(home_id)] = finished.get(int(home_id), 0) + 1
        finished[int(away_id)] = finished.get(int(away_id), 0) + 1
    captured = None
    if captured_at is not None:
        captured = captured_at.strftime("%d.%m %H:%M UTC")
    return build_group_table(
        group=group_name,
        rows=slots,
        home_id=match.home_team_id,
        away_id=match.away_team_id,
        home_name=match.home,
        away_name=match.away,
        home_goals=match.goals_home,
        away_goals=match.goals_away,
        status_short=match.status_short,
        included=_snapshot_includes_match(
            status_short=match.status_short,
            home_played=played.get(match.home_team_id, 0),
            away_played=played.get(match.away_team_id, 0),
            other_finished=finished,
            home_id=match.home_team_id,
            away_id=match.away_team_id,
        ),
        captured=captured,
    )


async def _events(session: AsyncSession, match: LiveMatch) -> list[DetailEvent]:
    assist = aliased(Player)
    rows = (
        await session.execute(
            select(
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
                FixtureEvent.team_id,
                FixtureEvent.event_type,
                FixtureEvent.detail,
                Player.name,
                assist.name,
                FixtureEvent.sort_order,
            )
            .outerjoin(Player, Player.id == FixtureEvent.player_id)
            .outerjoin(assist, assist.id == FixtureEvent.assist_player_id)
            .where(FixtureEvent.fixture_id == match.fixture_id)
            .order_by(
                FixtureEvent.minute,
                FixtureEvent.minute_extra.asc().nulls_first(),
                FixtureEvent.sort_order.asc().nulls_last(),
                FixtureEvent.id,
            )
        )
    ).all()
    events: list[DetailEvent] = []
    for minute, extra, team_id, event_type, detail, player, assist_name, _order in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        events.append(
            DetailEvent(
                minute=int(minute),
                extra=extra,
                side=side,
                event_type=str(event_type),
                detail=_blank(detail),
                player=_blank(player),
                assist=_blank(assist_name),
            )
        )
    return events


async def _attack_subs(session: AsyncSession, match: LiveMatch) -> list[AttackChange]:
    assist = aliased(Player)
    rows = (
        await session.execute(
            select(
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
                FixtureEvent.team_id,
                FixtureEvent.player_id,
                FixtureEvent.assist_player_id,
                Player.name,
                assist.name,
            )
            .outerjoin(Player, Player.id == FixtureEvent.player_id)
            .outerjoin(assist, assist.id == FixtureEvent.assist_player_id)
            .where(FixtureEvent.fixture_id == match.fixture_id)
            .where(func.lower(FixtureEvent.event_type) == "subst")
            .order_by(
                FixtureEvent.minute,
                FixtureEvent.minute_extra.asc().nulls_first(),
            )
        )
    ).all()
    spots = (
        await session.execute(
            select(
                FixtureLineupPlayer.team_id,
                FixtureLineupPlayer.player_id,
                FixtureLineupPlayer.position,
                FixtureLineupPlayer.grid,
                FixtureLineupPlayer.is_starter,
            ).where(FixtureLineupPlayer.fixture_id == match.fixture_id)
        )
    ).all()
    subs = [
        (
            int(minute),
            extra,
            int(team_id),
            None if off_id is None else int(off_id),
            None if on_id is None else int(on_id),
            _blank(off_name),
            _blank(on_name),
        )
        for minute, extra, team_id, off_id, on_id, off_name, on_name in rows
    ]
    lineup = [
        (
            int(team_id),
            int(player_id),
            _blank(position),
            _blank(grid),
            bool(starter),
        )
        for team_id, player_id, position, grid, starter in spots
    ]
    return list(
        recent_attack_changes(
            subs,
            lineup,
            home_id=match.home_team_id,
            away_id=match.away_team_id,
            home=match.home,
            away=match.away,
            elapsed=match.elapsed,
            extra=match.extra,
        )
    )


async def _stats(session: AsyncSession, match: LiveMatch) -> list[DetailStat]:
    rows = (
        await session.execute(
            select(
                FixtureStatistic.team_id,
                FixtureStatistic.stat_type,
                FixtureStatistic.stat_value,
            ).where(
                FixtureStatistic.fixture_id == match.fixture_id,
                FixtureStatistic.period == "FT",
            )
        )
    ).all()
    paired: dict[str, dict[str, str | None]] = {}
    for team_id, stat_type, value in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        paired.setdefault(str(stat_type), {"home": None, "away": None})[side] = _blank(
            value
        )
    ordered = [name for name in _STAT_ORDER if name in paired]
    ordered.extend(sorted(name for name in paired if name not in _STAT_ORDER))
    return [
        DetailStat(
            label=_STAT_LABELS.get(name, name),
            home=paired[name]["home"],
            away=paired[name]["away"],
        )
        for name in ordered
    ]


async def _lineup(session: AsyncSession, match: LiveMatch) -> tuple[
    list[DetailPlayer],
    list[DetailPlayer],
    list[DetailPlayer],
    list[DetailPlayer],
]:
    rows = (
        await session.execute(
            select(
                FixtureLineupPlayer.team_id,
                FixtureLineupPlayer.number,
                FixtureLineupPlayer.position,
                FixtureLineupPlayer.grid,
                FixtureLineupPlayer.is_starter,
                Player.name,
            )
            .outerjoin(Player, Player.id == FixtureLineupPlayer.player_id)
            .where(FixtureLineupPlayer.fixture_id == match.fixture_id)
        )
    ).all()
    home_xi: list[DetailPlayer] = []
    away_xi: list[DetailPlayer] = []
    home_bench: list[DetailPlayer] = []
    away_bench: list[DetailPlayer] = []
    keyed: list[tuple[str, bool, str, DetailPlayer]] = []
    for team_id, number, position, grid, starter, name in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        player = DetailPlayer(
            name=_blank(name) or "—",
            number=number,
            position=_blank(position),
            starter=bool(starter),
        )
        keyed.append((side, bool(starter), str(grid or ""), player))
    keyed.sort(
        key=lambda item: (
            item[0],
            not item[1],
            _grid_key(item[2]),
            item[3].number or 99,
        )
    )
    for side, starter, _grid, player in keyed:
        if side == "home":
            (home_xi if starter else home_bench).append(player)
        else:
            (away_xi if starter else away_bench).append(player)
    return home_xi, away_xi, home_bench, away_bench


async def _lineup_strength(
    session: AsyncSession,
    match: LiveMatch,
    starter_count: int,
    *,
    home: bool,
) -> LineupStrength | None:
    team_id = match.home_team_id if home else match.away_team_id
    if team_id <= 0 or starter_count < XI_SIZE:
        return None
    return await load_lineup_strength(
        session,
        team_id=team_id,
        fixture_id=match.fixture_id,
        league_id=match.league_id,
        season=match.season,
        kickoff=match.kickoff,
    )


async def _h2h(session: AsyncSession, fixture_id: int) -> list[DetailH2H]:
    home = aliased(Team)
    away = aliased(Team)
    rows = (
        await session.execute(
            select(
                Fixture.date,
                home.name,
                away.name,
                Fixture.goals_home,
                Fixture.goals_away,
                PredictionH2H.sort_order,
            )
            .join(Fixture, Fixture.id == PredictionH2H.h2h_fixture_id)
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .where(PredictionH2H.fixture_id == fixture_id)
            .order_by(
                PredictionH2H.sort_order.asc().nulls_last(),
                Fixture.date.desc().nulls_last(),
            )
            .limit(8)
        )
    ).all()
    items: list[DetailH2H] = []
    for kickoff, home_name, away_name, goals_home, goals_away, _order in rows:
        when = ""
        if isinstance(kickoff, datetime):
            when = kickoff.date().isoformat()
        score = "–"
        if goals_home is not None and goals_away is not None:
            score = f"{goals_home}–{goals_away}"
        items.append(
            DetailH2H(
                when=when,
                home=_blank(home_name) or "—",
                away=_blank(away_name) or "—",
                score=score,
            )
        )
    return items


async def _recent_results(
    session: AsyncSession, match: LiveMatch
) -> tuple[tuple[str, tuple[DetailH2H, ...]], ...]:
    groups: list[tuple[str, tuple[DetailH2H, ...]]] = []
    for team_id, name in (
        (match.home_team_id, match.home),
        (match.away_team_id, match.away),
    ):
        if not team_id:
            continue
        groups.append((name, tuple(await _team_results(session, match, team_id))))
    return tuple(group for group in groups if group[1])


async def _team_results(
    session: AsyncSession, match: LiveMatch, team_id: int
) -> list[DetailH2H]:
    home = aliased(Team)
    away = aliased(Team)
    rows = (
        await session.execute(
            select(
                Fixture.date,
                home.name,
                away.name,
                Fixture.goals_home,
                Fixture.goals_away,
            )
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .where(
                or_(
                    Fixture.home_team_id == team_id,
                    Fixture.away_team_id == team_id,
                )
            )
            .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
            .where(Fixture.id != match.fixture_id)
            .order_by(Fixture.date.desc().nulls_last(), Fixture.id.desc())
            .limit(FORM_LAST_MATCHES)
        )
    ).all()
    items: list[DetailH2H] = []
    for kickoff, home_name, away_name, goals_home, goals_away in rows:
        when = kickoff.date().isoformat() if isinstance(kickoff, datetime) else ""
        score = "–"
        if goals_home is not None and goals_away is not None:
            score = f"{goals_home}–{goals_away}"
        items.append(
            DetailH2H(
                when=when,
                home=_blank(home_name) or "—",
                away=_blank(away_name) or "—",
                score=score,
            )
        )
    return items


def _comparison_rows(raw: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    rows: list[tuple[str, str, str]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        home = value.get("home")
        away = value.get("away")
        if home is None and away is None:
            continue
        label = _COMPARE_LABELS.get(str(key), str(key))
        rows.append((label, str(home or "—"), str(away or "—")))
    return tuple(rows)


def _blank(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _grid_key(grid: str) -> tuple[int, int]:
    parts = grid.split(":")
    if len(parts) != 2:
        return (99, 99)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (99, 99)


async def _live_markets(session: AsyncSession, match: LiveMatch) -> list[OddsMarket]:
    latest = await session.scalar(
        select(func.max(FixtureOddsLive.captured_at)).where(
            FixtureOddsLive.fixture_id == match.fixture_id
        )
    )
    if latest is None:
        return []
    rows = (
        await session.execute(
            select(
                OddsLiveBet.name,
                FixtureOddsLive.value_label,
                FixtureOddsLive.handicap,
                FixtureOddsLive.odd,
                FixtureOddsLive.is_main,
                FixtureOddsLive.suspended,
            )
            .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
            .where(FixtureOddsLive.fixture_id == match.fixture_id)
            .where(FixtureOddsLive.captured_at == latest)
        )
    ).all()
    parsed: list[tuple[str, str, str, Decimal, bool | None, bool | None]] = []
    for market, label, handicap, odd, is_main, suspended in rows:
        if not market or not label:
            continue
        parsed.append(
            (
                str(market),
                str(label),
                "" if handicap is None else str(handicap),
                Decimal(odd),
                is_main,
                suspended,
            )
        )
    return list(
        select_display_markets(
            parsed,
            home=match.home,
            away=match.away,
            goals_home=match.goals_home,
            goals_away=match.goals_away,
        )
    )


async def _goal_price(session: AsyncSession, match: LiveMatch) -> GoalPrice | None:
    total = (match.goals_home or 0) + (match.goals_away or 0)
    line = total + 0.5
    latest = await session.scalar(
        select(func.max(FixtureOddsLive.captured_at)).where(
            FixtureOddsLive.fixture_id == match.fixture_id
        )
    )
    none_odd = _none_from_next_goal(match)
    over_odd = None
    if latest is not None:
        rows = (
            await session.execute(
                select(
                    OddsLiveBet.name,
                    FixtureOddsLive.value_label,
                    FixtureOddsLive.handicap,
                    FixtureOddsLive.odd,
                    FixtureOddsLive.suspended,
                )
                .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
                .where(FixtureOddsLive.fixture_id == match.fixture_id)
                .where(FixtureOddsLive.captured_at == latest)
            )
        ).all()
        if none_odd is None:
            none_odd = _none_odd(rows, total)
        over_odd = _over_odd(rows, line)
    ht_odd, ht_elapsed = await _ht_over(session, match.fixture_id, line, total)
    return read_goal_price(
        none_odd=none_odd,
        over_odd=over_odd,
        line=line,
        ht_odd=ht_odd,
        ht_elapsed=ht_elapsed,
    )


async def _ht_over(
    session: AsyncSession,
    fixture_id: int,
    line: float,
    total: int,
) -> tuple[Decimal | None, int | None]:
    rows = (
        await session.execute(
            select(
                FixtureOddsLive.odd,
                FixtureOddsLive.handicap,
                FixtureOddsLive.elapsed_minutes,
            )
            .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
            .where(FixtureOddsLive.fixture_id == fixture_id)
            .where(func.lower(OddsLiveBet.name).in_(tuple(_OVER_MARKETS)))
            .where(func.lower(FixtureOddsLive.value_label) == "over")
            .where(FixtureOddsLive.elapsed_minutes >= 45)
            .where(FixtureOddsLive.home_goals + FixtureOddsLive.away_goals == total)
            .order_by(FixtureOddsLive.captured_at.asc())
        )
    ).all()
    for odd, handicap, elapsed in rows:
        number = _handicap_num("" if handicap is None else str(handicap))
        if number is None or abs(number - line) >= 0.01:
            continue
        return Decimal(odd), None if elapsed is None else int(elapsed)
    return None, None
