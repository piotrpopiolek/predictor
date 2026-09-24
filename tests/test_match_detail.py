from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predictor.services.live_board import (
    LiveMatch,
    NextGoalOdds,
    PrematchOdds,
    TeamGoalForm,
)
from predictor.services.match_detail import (
    DetailEvent,
    DetailH2H,
    DetailPlayer,
    DetailStat,
    MatchDetail,
    StandingSlot,
    _goal_price,
    _live_markets,
    _none_odd,
    _over_odd,
    _snapshot_includes_match,
    build_group_table,
    read_goal_price,
    recent_attack_changes,
    render_match_html,
    render_match_missing,
    select_display_markets,
)


def _detail() -> MatchDetail:
    match = LiveMatch(
        fixture_id=42,
        country="Iran",
        league="Azadegan League",
        round="Regular Season - 2",
        home="Mes Kerman",
        away="Saipa",
        home_logo=None,
        away_logo=None,
        goals_home=0,
        goals_away=1,
        status_short="2H",
        status_long="Second Half",
        elapsed=90,
        extra=1,
        venue="Shahid Bahonar Stadium",
        venue_city="Kerman",
        ht_home=0,
        ht_away=1,
        home_formation="4-4-2",
        away_formation="4-3-3",
        prematch=PrematchOdds("2.10", "3.20", "3.40", "Match Winner", "Bet365"),
        form_home=TeamGoalForm(5, 0.4, 40.0),
        prediction_pct_home=45.0,
        prediction_pct_away=28.0,
    )
    return MatchDetail(
        match=match,
        events=(DetailEvent(55, None, "away", "Goal", "Normal Goal", "Saipa", None),),
        stats=(DetailStat("Posiadanie", "48%", "52%"),),
        home_xi=(DetailPlayer("Ali", 9, "F", True),),
        away_xi=(DetailPlayer("Reza", 10, "M", True),),
        home_bench=(),
        away_bench=(DetailPlayer("Omid", 17, None, False),),
        advice="Double chance : Saipa or draw",
        expected_home="1.1",
        expected_away="1.4",
        pct_draw="27%",
        comparison=(("Forma", "40%", "60%"),),
        h2h=(DetailH2H("2026-05-01", "Saipa", "Mes Kerman", "1–0"),),
    )


def test_read_goal_price_treats_over_line_as_another_goal() -> None:
    price = read_goal_price(
        none_odd=Decimal("3.40"),
        over_odd=Decimal("1.25"),
        line=1.5,
        ht_odd=Decimal("1.45"),
        ht_elapsed=46,
    )
    assert price is not None
    assert price.none_odd == "3.40"
    assert price.none_implied == "29%"
    assert price.line == "1,5"
    assert price.over_odd == "1.25"
    assert "1.45 → 1.25" in (price.move or "")
    assert "więcej sytuacji" in (price.move or "")
    flat = read_goal_price(
        none_odd=None,
        over_odd=Decimal("1.45"),
        line=1.5,
        ht_odd=Decimal("1.45"),
        ht_elapsed=46,
    )
    assert flat is not None
    assert flat.move is None


def test_recent_attack_changes_marks_striker_on_and_only_winger_off() -> None:
    spots = [
        (1, 11, "F", "4:2", True),
        (1, 10, "F", "3:1", True),
        (1, 20, "F", None, False),
    ]
    changes = recent_attack_changes(
        [(78, None, 1, 10, 20, "Wing", "Striker")],
        spots,
        home_id=1,
        away_id=2,
        home="Home",
        away="Away",
        elapsed=85,
        extra=None,
    )
    assert len(changes) == 1
    assert "schodzi Wing (skrzydłowy)" in changes[0].summary
    assert "wchodzi Striker (napastnik)" in changes[0].summary
    assert changes[0].effect is not None
    assert "Wszedł napastnik" in changes[0].effect
    assert "jedyne skrzydło" in changes[0].effect
    early = recent_attack_changes(
        [(40, None, 1, 10, 20, "Wing", "Striker")],
        spots,
        home_id=1,
        away_id=2,
        home="Home",
        away="Away",
        elapsed=85,
        extra=None,
    )
    assert early == ()


def test_select_display_markets_keeps_main_lines_and_next_goal() -> None:
    rows = [
        ("Fulltime Result", "Home", "", Decimal("1.615"), None, False),
        ("Fulltime Result", "Draw", "", Decimal("3.5"), None, False),
        ("Fulltime Result", "Away", "", Decimal("6"), None, False),
        ("Over/Under Line", "Over", "2.5", Decimal("2"), True, False),
        ("Over/Under Line", "Under", "2.5", Decimal("1.8"), True, False),
        ("Over/Under Line", "Over", "3", Decimal("3.1"), False, False),
        (
            "Which team will score the 2nd goal?",
            "Away",
            "",
            Decimal("2.1"),
            None,
            False,
        ),
        ("Final Score", "1-0", "", Decimal("7"), None, False),
        ("Double Chance", "Home or Draw", "", Decimal("1.1"), None, True),
    ]
    markets = select_display_markets(
        rows, home="Uzbekistan", away="Iran", goals_home=1, goals_away=0
    )
    titles = [market.title for market in markets]
    assert titles == ["Wynik", "Powyżej / poniżej", "Kto strzeli 2. gola"]
    assert markets[0].quotes[0].label == "Uzbekistan"
    assert markets[0].quotes[0].odd == "1.62"
    assert markets[1].quotes[0].label == "Powyżej 2.5"
    assert len(markets[1].quotes) == 2
    assert all(quote.odd != "1.10" for market in markets for quote in market.quotes)


def test_render_match_html_shows_collected_sections() -> None:
    html = render_match_html(
        _detail(), generated_at=datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    )
    assert "Mes Kerman" in html
    assert "Saipa" in html
    assert "0–1" in html
    assert "90+1" in html
    assert "Shahid Bahonar Stadium" in html
    assert "HT 0–1" in html
    assert "Zdarzenia" in html
    assert "Normal Goal" in html
    assert "Posiadanie" in html
    assert "Składy" in html
    assert "Ali" in html
    assert "Ławka" in html
    assert "Prognoza" in html
    assert "Double chance" in html
    assert "Otwarcie" in html
    assert "Bezpośrednie" in html
    assert 'href="/live"' in html


def test_render_match_missing() -> None:
    html = render_match_missing()
    assert "Nie ma takiego meczu" in html


def _group_b() -> list[StandingSlot]:
    return [
        StandingSlot(1, "Iran U23", 4, 2, 3, 1, 1),
        StandingSlot(2, "China PR U23", 4, 2, 2, 1, 2),
        StandingSlot(3, "Korea DPR U23", 1, 2, 1, 2, 3),
        StandingSlot(4, "UAE U23", 1, 2, 1, 3, 4),
    ]


def test_live_score_moves_the_group() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="2H",
        included=False,
        captured="23.09 00:00 UTC",
    )
    assert table.projected is not None
    assert [row.slot.name for row in table.before] == [
        "Iran U23",
        "China PR U23",
        "Korea DPR U23",
        "UAE U23",
    ]
    assert [row.slot.name for row in table.projected] == [
        "Iran U23",
        "China PR U23",
        "Korea DPR U23",
        "UAE U23",
    ]
    korea = table.projected[2]
    assert korea.slot.points == 4
    assert korea.slot.gd == 0
    assert korea.delta == 0
    assert table.projected[1].slot.gd == 1
    assert table.moves == "Miejsca się nie zmieniają."
    assert table.splits[2].active
    assert table.split == "Korea DPR U23 +3, Iran U23 +0"


def test_two_goal_win_reorders_the_group() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=0,
        away_goals=2,
        status_short="2H",
        included=False,
        captured=None,
    )
    assert table.projected is not None
    assert [row.slot.name for row in table.projected] == [
        "Korea DPR U23",
        "China PR U23",
        "Iran U23",
        "UAE U23",
    ]
    deltas = {row.slot.name: row.delta for row in table.projected}
    assert deltas["Korea DPR U23"] == 2
    assert deltas["Iran U23"] == -2
    assert table.moves is not None
    assert "Korea DPR U23 awansuje na 1." in table.moves
    assert "Iran U23 spada na 3." in table.moves


def test_kickoff_shows_splits_without_a_live_table() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=None,
        away_goals=None,
        status_short="NS",
        included=False,
        captured=None,
    )
    assert table.projected is None
    assert not any(item.active for item in table.splits)
    assert table.splits[0].home_points == 7
    assert table.splits[1].home_points == 5
    assert table.splits[2].away_points == 4


def test_finished_table_is_not_counted_twice() -> None:
    live = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="2H",
        included=False,
        captured=None,
    )
    assert live.projected is not None
    finished = build_group_table(
        group="Group B",
        rows=[row.slot for row in live.projected],
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="FT",
        included=True,
        captured=None,
    )
    assert [row.slot.points for row in finished.before] == [
        row.slot.points for row in live.before
    ]
    assert finished.projected is not None
    assert [row.slot.name for row in finished.projected] == [
        row.slot.name for row in live.projected
    ]


def test_snapshot_includes_only_a_finished_match() -> None:
    others = {1: 2, 3: 2}
    assert not _snapshot_includes_match(
        status_short="2H",
        home_played=2,
        away_played=2,
        other_finished=others,
        home_id=1,
        away_id=3,
    )
    assert _snapshot_includes_match(
        status_short="FT",
        home_played=3,
        away_played=3,
        other_finished=others,
        home_id=1,
        away_id=3,
    )
    assert not _snapshot_includes_match(
        status_short="FT",
        home_played=2,
        away_played=2,
        other_finished=others,
        home_id=1,
        away_id=3,
    )


def test_render_match_html_shows_table_movement() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=0,
        away_goals=2,
        status_short="2H",
        included=False,
        captured="23.09 00:00 UTC",
    )
    detail = replace(
        _detail(),
        match=replace(
            _detail().match,
            home="Iran U23",
            away="Korea DPR U23",
            home_team_id=1,
            away_team_id=3,
            goals_home=0,
            goals_away=2,
            status_short="2H",
        ),
        table=table,
    )
    html = render_match_html(
        detail, generated_at=datetime(2026, 9, 23, 6, 40, tzinfo=UTC)
    )
    assert "Tabela" in html
    assert "Otwarcie" in html
    assert "Przy wyniku 0–2" in html
    assert "Korea DPR U23 awansuje na 1." in html
    assert "Iran U23 spada na 3." in html
    assert 'class="split-card active"' in html


def test_goal_price_and_live_markets_render() -> None:
    rows = [
        (
            "Which team will score the 2nd goal?",
            "No goal",
            "",
            Decimal("2.40"),
            None,
            False,
        ),
        ("Which team will score the 2nd goal?", "1", "", Decimal("1.80"), None, False),
        ("Over/Under Line", "Over", "1.5", Decimal("1.70"), True, False),
        ("Over/Under Line", "Under", "1.5", Decimal("2.10"), True, False),
        ("Match Goals", "Over", "2.5", Decimal("1.90"), False, False),
        ("Fulltime Result", "Home", "", Decimal("1.50"), True, False),
        ("Fulltime Result", "Draw", "", Decimal("3.40"), True, False),
        ("Fulltime Result", "Away", "", Decimal("6.00"), True, False),
        ("Both Teams To Score", "Yes", "", Decimal("1.80"), True, False),
        ("Both Teams To Score", "No", "", Decimal("1.95"), True, False),
        ("Double Chance", "Home/Draw", "", Decimal("1.20"), True, False),
        ("Asian Handicap", "Home", "-0.5", Decimal("1.90"), True, False),
        ("Suspended Market", "Home", "", Decimal("1.10"), True, True),
    ]
    markets = select_display_markets(
        rows, home="Iran", away="Korea", goals_home=1, goals_away=0
    )
    assert any(market.title == "Wynik" for market in markets)
    assert any("gola" in market.title for market in markets)
    assert _none_odd(
        [
            (
                "Which team will score the 2nd goal?",
                "No goal",
                "",
                Decimal("2.40"),
                False,
            )
        ],
        1,
    ) == Decimal("2.40")
    assert (
        _none_odd(
            [
                (
                    "Which team will score the 2nd goal?",
                    "No goal",
                    "",
                    Decimal("2.40"),
                    True,
                )
            ],
            1,
        )
        is None
    )
    assert _over_odd(
        [("Over/Under Line", "Over", "1.5", Decimal("1.70"), False)],
        1.5,
    ) == Decimal("1.70")
    price = read_goal_price(
        none_odd=Decimal("2.40"),
        over_odd=Decimal("1.70"),
        line=1.5,
        ht_odd=Decimal("2.10"),
        ht_elapsed=67,
    )
    assert price is not None
    assert "więcej sytuacji" in (price.move or "")
    quieter = read_goal_price(
        none_odd=None,
        over_odd=Decimal("2.20"),
        line=1.5,
        ht_odd=Decimal("1.70"),
        ht_elapsed=None,
    )
    assert quieter is not None
    assert "mniej sytuacji" in (quieter.move or "")
    assert (
        read_goal_price(
            none_odd=None, over_odd=None, line=1.5, ht_odd=None, ht_elapsed=None
        )
        is None
    )
    detail = replace(
        _detail(),
        match=replace(
            _detail().match,
            goals_home=1,
            goals_away=0,
            next_goal=NextGoalOdds("1.80", "2.40", "3.10", "2nd goal"),
            prematch=PrematchOdds("2.10", "3.20", "3.40", "Match Winner", "Bet365"),
        ),
        markets=markets,
        goal_price=price,
    )
    html = render_match_html(
        detail, generated_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    )
    assert "Czy padnie gol" in html
    assert "Brak gola" in html
    assert "Powyżej" in html
    assert "Otwarcie" in html
    assert "Wynik" in html


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _PriceSession:
    def __init__(self, latest: datetime | None, batches: list[list[object]]) -> None:
        self._latest = latest
        self._batches = batches

    async def scalar(self, stmt: object) -> datetime | None:
        del stmt
        return self._latest

    async def execute(self, stmt: object) -> _Rows:
        del stmt
        if not self._batches:
            return _Rows([])
        return _Rows(self._batches.pop(0))


@pytest.mark.asyncio
async def test_goal_price_reads_live_rows_and_halftime_line() -> None:
    match = replace(_detail().match, goals_home=0, goals_away=0, next_goal=None)
    session = _PriceSession(
        datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        [
            [
                (
                    "Which team will score the 1st goal?",
                    "No goal",
                    "",
                    Decimal("3.20"),
                    False,
                ),
                ("Over/Under Line", "Over", "0.5", Decimal("1.40"), False),
                (None, "Over", "0.5", Decimal("1.10"), False),
            ],
            [(Decimal("1.90"), "0.5", 45), (Decimal("1.80"), "bad", 50)],
        ],
    )
    price = await _goal_price(session, match)  # type: ignore[arg-type]
    assert price is not None
    assert price.none_odd == "3.20"
    assert price.over_odd == "1.40"
    markets = await _live_markets(session, match)  # type: ignore[arg-type]
    assert markets == []
