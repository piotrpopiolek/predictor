from __future__ import annotations

from datetime import UTC, datetime

from predictor.services.lineup_strength import (
    LABEL_INJURED,
    LABEL_MIXED,
    LABEL_RESERVES,
    LABEL_STRONGEST,
    LABEL_THIN,
    HistoryMatch,
    PlayerRole,
    SeasonStat,
    SquadPlayer,
    _role,
    _share_at_least,
    assess_lineup,
    load_lineup_strength,
    select_season_stats,
)

SEASON = 2026
KICKOFF = datetime(2026, 9, 20, 18, tzinfo=UTC)
CORES = range(1, 12)
RESERVES = range(12, 23)
_DAYS = (1, 3, 5, 7, 9, 11, 13, 15)


def _names(*player_ids: int) -> dict[int, str]:
    return {player_id: f"P{player_id}" for player_id in player_ids}


def _squad(
    starters: range | tuple[int, ...],
    bench: tuple[int, ...] = (),
) -> tuple[SquadPlayer, ...]:
    rows = [SquadPlayer(player_id, True) for player_id in starters]
    rows.extend(SquadPlayer(player_id, False) for player_id in bench)
    return tuple(rows)


def _match(
    fixture_id: int,
    day: int,
    starters: range | tuple[int, ...] | list[int],
    bench: range | tuple[int, ...] | list[int] = (),
    *,
    league: str = "League",
    season: int = SEASON,
) -> HistoryMatch:
    players = tuple((player_id, True) for player_id in starters)
    players += tuple((player_id, False) for player_id in bench)
    return HistoryMatch(
        fixture_id=fixture_id,
        kickoff=datetime(2026, 9, day, 12, tzinfo=UTC),
        season=season,
        league_type=league,
        players=players,
    )


def _league_window() -> tuple[HistoryMatch, ...]:
    return tuple(_match(100 + day, day, CORES, RESERVES) for day in _DAYS)


def test_regular_eleven_is_the_strongest_side() -> None:
    history = _league_window() + (
        _match(200, 25, RESERVES, CORES),
        _match(201, 12, RESERVES, CORES, league="Cup"),
        HistoryMatch(202, None, SEASON, "League", tuple((1, True) for _ in range(11))),
    )
    result = assess_lineup(
        history,
        (),
        _squad(CORES),
        set(),
        _names(*CORES, *RESERVES),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_STRONGEST
    assert result.note == "z ostatnich 8 meczów ligowych"
    assert result.missing == ()


def test_cup_eleven_from_the_bench_is_reserves() -> None:
    result = assess_lineup(
        _league_window(),
        (),
        _squad(RESERVES),
        set(),
        _names(*CORES, *RESERVES),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_RESERVES
    assert result.note == "z ostatnich 8 meczów ligowych"
    assert "P1 (poza kadrą)" in result.missing
    assert len(result.missing) == 11


def test_short_history_without_usable_season_stats_stays_unlabeled() -> None:
    history = tuple(_match(100 + day, day, CORES) for day in (1, 3, 5, 7))
    stats = tuple(
        SeasonStat(player_id, lineups=2, sub_bench=2, league_id=1)
        for player_id in CORES
    )
    result = assess_lineup(
        history,
        stats,
        _squad(CORES),
        set(),
        _names(*CORES),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_THIN
    assert result.note is None
    short = assess_lineup(
        _league_window(),
        (),
        _squad(range(1, 11)),
        set(),
        {},
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert short.label is None


def test_mixed_side_is_injuries_when_every_missing_regular_is_hurt() -> None:
    starters = tuple(range(1, 7)) + tuple(range(12, 17))
    names = _names(*CORES, *RESERVES)
    hurt = assess_lineup(
        _league_window(),
        (),
        _squad(starters),
        set(range(7, 12)),
        names,
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert hurt.label == LABEL_INJURED
    assert "P7 (kontuzja)" in hurt.missing
    rested = assess_lineup(
        _league_window(),
        (),
        _squad(starters, bench=(11,)),
        set(range(7, 11)),
        names,
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert rested.label == LABEL_MIXED
    assert "P11 (na ławce)" in rested.missing


def test_season_stats_cover_a_short_lineup_history() -> None:
    history = tuple(_match(100 + day, day, CORES) for day in (1, 3))
    stats = tuple(
        SeasonStat(player_id, lineups=10, sub_bench=0, league_id=9)
        for player_id in CORES
    )
    result = assess_lineup(
        history,
        stats,
        _squad(CORES),
        set(),
        _names(*CORES),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_STRONGEST
    assert result.note == "ze statystyk sezonu"


def test_other_competitions_fill_a_short_league_sample() -> None:
    league = tuple(_match(100 + day, day, CORES, RESERVES) for day in (1, 3, 5))
    cups = tuple(
        _match(200 + day, day, CORES, RESERVES, league="Cup")
        for day in (2, 4, 6, 8, 10)
    )
    result = assess_lineup(
        league + cups,
        (),
        _squad(CORES),
        set(),
        _names(*CORES),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_STRONGEST
    assert result.note == "z ostatnich 8 meczów"


def test_one_start_and_a_frequent_bench_is_rotation() -> None:
    group = list(range(1, 12))
    start_on = {
        0: [1, 2],
        1: [3, 4],
        2: [5, 6],
        3: [7, 8],
        4: [9, 10],
        5: [11],
        6: [],
        7: [],
    }

    def history(*, bench: bool) -> tuple[HistoryMatch, ...]:
        bench_count = dict.fromkeys(group, 0)
        filler_id = 200
        filler_used: dict[int, int] = {}
        matches: list[HistoryMatch] = []
        for index, day in enumerate(_DAYS):
            starters = list(start_on[index])
            while len(starters) < 11:
                if filler_used.get(filler_id, 0) >= 2:
                    filler_id += 1
                    continue
                starters.append(filler_id)
                filler_used[filler_id] = filler_used.get(filler_id, 0) + 1
                if filler_used[filler_id] >= 2:
                    filler_id += 1
            bench_ids: list[int] = []
            if bench:
                for player_id in group:
                    if player_id in starters or bench_count[player_id] >= 4:
                        continue
                    bench_ids.append(player_id)
                    bench_count[player_id] += 1
            matches.append(_match(300 + index, day, starters, bench_ids))
        return tuple(matches)

    rotated = assess_lineup(
        history(bench=True),
        (),
        _squad(tuple(group)),
        set(),
        _names(*group),
        season=SEASON,
        kickoff=KICKOFF,
    )
    unused = assess_lineup(
        history(bench=False),
        (),
        _squad(tuple(group)),
        set(),
        _names(*group),
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert rotated.label == LABEL_MIXED
    assert unused.label == LABEL_RESERVES


def test_undated_league_matches_count_when_this_kickoff_is_unknown() -> None:
    history = tuple(
        HistoryMatch(
            fixture_id=day,
            kickoff=None,
            season=SEASON,
            league_type="League",
            players=tuple((player_id, True) for player_id in CORES),
        )
        for day in _DAYS
    )
    result = assess_lineup(
        history,
        (),
        _squad(CORES),
        set(),
        _names(*CORES),
        season=SEASON,
        kickoff=None,
    )
    assert result.label == LABEL_STRONGEST


def test_no_appearances_is_unknown_and_empty_share_is_false() -> None:
    assert _role(0, 0, 5) is PlayerRole.UNKNOWN
    assert _share_at_least(1, 0, 1, 2) is False


def test_select_season_stats_prefers_this_league_then_the_busiest() -> None:
    rows = (
        SeasonStat(1, 1, 0, league_id=4),
        SeasonStat(2, 9, 0, league_id=7),
        SeasonStat(3, 3, 0, league_id=4),
    )
    picked = select_season_stats(rows, prefer_league_id=4)
    assert {row.player_id for row in picked} == {1, 3}
    busiest = select_season_stats(rows, prefer_league_id=None)
    assert {row.player_id for row in busiest} == {2}
    assert select_season_stats((), prefer_league_id=None) == ()


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


class _Session:
    def __init__(
        self, batches: list[list[tuple[object, ...]]], league_type: str | None
    ) -> None:
        self._batches = batches
        self._league_type = league_type

    async def scalar(self, _statement: object) -> str | None:
        return self._league_type

    async def execute(self, _statement: object) -> _Rows:
        return _Rows(self._batches.pop(0))


def _history_rows() -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = [(1, "not-a-date", 2026, None, 99, True)]
    for day in _DAYS:
        kickoff = datetime(2026, 9, day, 12, tzinfo=UTC)
        for player_id in CORES:
            rows.append((100 + day, kickoff, 2026, "League", player_id, True))
        for player_id in RESERVES:
            rows.append((100 + day, kickoff, 2026, "League", player_id, False))
    return rows


async def test_load_lineup_strength_reads_rows_and_skips_empty_names() -> None:
    names = [(player_id, f"P{player_id}") for player_id in CORES]
    names.append((12, None))
    names.append((13, "  "))
    session = _Session(
        [
            _history_rows(),
            [(8, 99, None, None), *[(9, player_id, 10, 0) for player_id in CORES]],
            [(player_id, True) for player_id in CORES],
            [(99,)],
            names,
        ],
        " League ",
    )
    result = await load_lineup_strength(
        session,  # type: ignore[arg-type]
        team_id=1,
        fixture_id=50,
        league_id=9,
        season=SEASON,
        kickoff=KICKOFF,
    )
    assert result.label == LABEL_STRONGEST
    assert result.note == "z ostatnich 8 meczów ligowych"
    empty = _Session([[], [], [], []], None)
    unlabeled = await load_lineup_strength(
        empty,  # type: ignore[arg-type]
        team_id=1,
        fixture_id=50,
        league_id=9,
        season=SEASON,
        kickoff=None,
    )
    assert unlabeled.label is None
    assert empty._batches == []
