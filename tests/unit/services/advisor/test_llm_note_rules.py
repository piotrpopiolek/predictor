"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from predictor.services.advisor.candidates import MatchSnapshot
from predictor.services.advisor.domain import PlayLimits
from predictor.services.advisor.llm_note import (
    LlmDecisionNote,
    LlmGap,
    LlmNote,
    _apply_lone_weak,
    llm_prompt_gaps,
)
from predictor.services.advisor.sql import OverUnderRow
from predictor.ui.llm_note import render_llm_note

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)

LIMITS = PlayLimits(
    max_minute_regulation=80,
    max_minute_extra=110,
    max_quote_age=timedelta(seconds=300),
)


def _match(**overrides: object) -> MatchSnapshot:
    values: dict[str, object] = {
        "fixture_id": 1,
        "status_short": "2H",
        "elapsed": 60,
        "extra": None,
        "goals_home": 0,
        "goals_away": 1,
        "extratime_home": None,
        "extratime_away": None,
        "rule": "",
        "favorite_odd": Decimal("1.50"),
    }
    values.update(overrides)
    return MatchSnapshot(**values)  # type: ignore[arg-type]


def _row(
    odd: str = "1.500",
    *,
    tradable: bool = True,
    age: int = 20,
    handicap: str = "1.5",
    home_goals: int | None = 0,
    away_goals: int | None = 1,
) -> OverUnderRow:
    return OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap=handicap,
        odd=Decimal(odd),
        tradable=tradable,
        captured_at=NOW - timedelta(seconds=age),
        home_goals=home_goals,
        away_goals=away_goals,
    )


def _gaps(
    match: MatchSnapshot, rows: tuple[OverUnderRow, ...] = ()
) -> tuple[LlmGap, ...]:
    return llm_prompt_gaps(
        match,
        rows,
        is_candidate=True,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
    )


def test_a_fresh_second_half_line_has_no_llm_gaps() -> None:
    assert _gaps(_match(), (_row(),)) == ()


def test_extra_time_uses_et_goals_and_et_market() -> None:
    from predictor.services.advisor.domain import EXTRA_TIME_OVER_UNDER

    et = _match(
        status_short="ET",
        elapsed=105,
        goals_home=1,
        goals_away=0,
        extratime_home=1,
        extratime_away=0,
    )
    wrong_market = _row(handicap="1.5")
    assert LlmGap.NO_LINE in _gaps(et, (wrong_market,))
    priced = OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.500"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=20),
        market=EXTRA_TIME_OVER_UNDER,
        home_goals=1,
        away_goals=0,
    )
    assert _gaps(et, (priced,)) == ()
    late = _match(
        status_short="ET",
        elapsed=111,
        goals_home=1,
        goals_away=0,
        extratime_home=1,
        extratime_away=0,
    )
    assert LlmGap.MINUTE in _gaps(late, (priced,))


def test_missing_gates_are_listed_together() -> None:
    gaps = llm_prompt_gaps(
        _match(status_short="1H", elapsed=30),
        (_row(odd="1.190", age=400),),
        is_candidate=False,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset({1}),
        rejected=frozenset(),
    )
    assert gaps == (
        LlmGap.NOT_CANDIDATE,
        LlmGap.NOT_SECOND_HALF,
        LlmGap.STALE,
        LlmGap.ODD_LOW,
        LlmGap.OPEN_COUPON,
    )


def test_note_renders_gaps_or_the_decision() -> None:
    blocked = render_llm_note(
        LlmNote(
            gaps=(LlmGap.NOT_SECOND_HALF, LlmGap.NO_LINE),
            minute_limit=80,
            line="1.5",
            decision=None,
        )
    )
    assert "Do LLM brakuje" in blocked
    assert "status inny niż 2H/ET/BT" in blocked
    assert "brak Over 1.5" in blocked
    gates = render_llm_note(
        LlmNote(
            gaps=(
                LlmGap.PERIOD_GOALS,
                LlmGap.FAVORITE_ODD,
                LlmGap.SHORT_LATE,
                LlmGap.LONE_WEAK,
            ),
            minute_limit=80,
            line="1.5",
            decision=None,
        )
    )
    assert "3 lub więcej goli w okresie" in gates
    assert "faworyt bez kursu prematch albo z kursem od 2,00" in gates
    assert "kurs poniżej 1,200 po 60. minucie" in gates
    assert "jedyny kandydat za słaby na singiel" in gates
    ready = render_llm_note(
        LlmNote(
            gaps=(),
            minute_limit=80,
            line="1.5",
            decision=LlmDecisionNote(
                decision="bet",
                rationale="Sheffield press",
                goals_home=0,
                goals_away=1,
                same_state=True,
            ),
        )
    )
    assert "LLM: graj przy 0:1" in ready
    assert "Sheffield press" in ready
    plain = render_llm_note(
        LlmNote(
            gaps=(),
            minute_limit=80,
            line=None,
            decision=LlmDecisionNote("reject", "", 0, 1, True),
        )
    )
    assert plain.endswith("LLM: odrzuć przy 0:1</p>")
    paused = render_llm_note(
        LlmNote(
            gaps=(),
            minute_limit=80,
            line="1.5",
            decision=None,
            strategy_paused=True,
        )
    )
    assert "wstrzymana" in paused
    assert "W puli LLM" not in paused
    late = render_llm_note(
        LlmNote(
            gaps=(LlmGap.MINUTE,),
            minute_limit=80,
            line=None,
            decision=LlmDecisionNote(
                decision="wait",
                rationale="  " + ("press " * 40),
                goals_home=1,
                goals_away=1,
                same_state=False,
            ),
        )
    )
    assert "minuta powyżej limitu 80" in late
    assert "Ostatnia decyzja LLM: czekaj przy 1:1" in late
    assert late.endswith("…</p>") or "…" in late


def test_prompt_gate_gaps_mirror_domain() -> None:
    assert LlmGap.PERIOD_GOALS in _gaps(
        _match(goals_home=2, goals_away=1),
        (_row(handicap="3.5", odd="1.700", home_goals=2, away_goals=1),),
    )
    assert LlmGap.FAVORITE_ODD in _gaps(_match(favorite_odd=None), (_row(),))
    assert LlmGap.FAVORITE_ODD in _gaps(_match(favorite_odd=Decimal("2.00")), (_row(),))
    assert LlmGap.SHORT_LATE in _gaps(_match(elapsed=61), (_row(odd="1.190"),))
    assert LlmGap.SHORT_LATE not in _gaps(_match(elapsed=60), (_row(odd="1.400"),))


def test_apply_lone_weak_marks_only_a_sole_weak_ready_match() -> None:
    floor = Decimal("1.600")
    weak = _match(fixture_id=1)
    partner = _match(fixture_id=2)
    weak_row = _row(odd="1.700")
    strong_row = _row(odd="1.850")
    notes = {
        1: LlmNote(gaps=(), minute_limit=80, line="1.5", decision=None),
    }
    _apply_lone_weak(notes, {1: weak}, {1: (weak_row,)}, min_combined_odd=floor)
    assert notes[1].gaps == (LlmGap.LONE_WEAK,)

    pair = {
        1: LlmNote(gaps=(), minute_limit=80, line="1.5", decision=None),
        2: LlmNote(gaps=(), minute_limit=80, line="1.5", decision=None),
    }
    _apply_lone_weak(
        pair,
        {1: weak, 2: partner},
        {1: (weak_row,), 2: (strong_row,)},
        min_combined_odd=floor,
    )
    assert pair[1].gaps == ()
    assert pair[2].gaps == ()

    strong_only = {
        1: LlmNote(gaps=(), minute_limit=80, line="1.5", decision=None),
    }
    _apply_lone_weak(
        strong_only,
        {1: _match(fixture_id=1)},
        {1: (strong_row,)},
        min_combined_odd=floor,
    )
    assert strong_only[1].gaps == ()


def test_remaining_prompt_gates() -> None:
    assert LlmGap.MINUTE in _gaps(_match(elapsed=None), (_row(),))
    assert LlmGap.MINUTE in _gaps(_match(elapsed=90), (_row(),))
    assert LlmGap.PENALTIES in llm_prompt_gaps(
        _match(status_short="P"),
        (),
        is_candidate=True,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
    )
    assert LlmGap.SUSPENDED in _gaps(_match(), (_row(tradable=False),))
    assert LlmGap.NO_LINE in _gaps(_match(goals_home=None, goals_away=None))
    naive = OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.500"),
        tradable=True,
        captured_at=datetime(2026, 10, 24, 15, 0),
        home_goals=0,
        away_goals=1,
    )
    future = OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.500"),
        tradable=True,
        captured_at=NOW + timedelta(minutes=5),
        home_goals=0,
        away_goals=1,
    )
    assert LlmGap.STALE in _gaps(_match(), (naive,))
    assert LlmGap.STALE in _gaps(_match(), (future,))
    rejected = llm_prompt_gaps(
        _match(),
        (_row(),),
        is_candidate=True,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset({(1, "regulation", 0, 1, 2)}),
    )
    assert rejected == (LlmGap.REJECTED,)
    assert (
        llm_prompt_gaps(
            _match(goals_home=None),
            (),
            is_candidate=True,
            limits=LIMITS,
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset({(1, "regulation", 0, 1, 2)}),
        )
        != ()
    )
    empty = LlmNote(gaps=(), minute_limit=80, line="1.5", decision=None)
    assert empty.as_dict()["strategy_paused"] is False
    decided = LlmNote(
        gaps=(),
        minute_limit=80,
        line="1.5",
        decision=LlmDecisionNote("bet", "chasing", 0, 1, True),
    )
    payload = decided.as_dict()["decision"]
    assert isinstance(payload, dict)
    assert payload["decision"] == "bet"
    assert LlmGap.NO_LINE in _gaps(_match(), (_row(handicap="2.5"),))
