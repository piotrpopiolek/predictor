"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from predictor.constants import PAYOUT_KEEP
from predictor.services.advisor.candidates import (
    MatchSnapshot,
    lone_candidate_allowed,
    playable_candidate,
)
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import (
    MIN_ANALYSIS_ODD,
    PlayLimits,
    over_line_matches,
)
from predictor.services.advisor.prompts.cycle import (
    PROMPT_VERSION,
    _price,
    render_cycle_prompt,
)
from predictor.services.advisor.sql import (
    FirstOver,
)
from predictor.services.advisor.tick import (
    CandidateWindow,
    _candidate_body_odd,
)

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)


def _limits() -> PlayLimits:
    return PlayLimits(
        max_minute_regulation=80,
        max_minute_extra=110,
        max_quote_age=timedelta(seconds=300),
    )


def _match(**overrides: object) -> MatchSnapshot:
    values: dict[str, object] = {
        "fixture_id": 7,
        "status_short": "2H",
        "elapsed": 70,
        "extra": None,
        "goals_home": 0,
        "goals_away": 1,
        "extratime_home": None,
        "extratime_away": None,
        "rule": "favorite_losing",
        "favorite_odd": Decimal("1.50"),
    }
    values.update(overrides)
    return MatchSnapshot(**values)  # type: ignore[arg-type]


def test_over_line_matches_the_driver_total_and_not_another() -> None:
    assert over_line_matches("Over", "1.5", 1)
    assert over_line_matches("Over", "1.50", 1)
    assert over_line_matches("Over 1.5", "", 1)
    assert not over_line_matches("Over", "2.5", 1)
    assert not over_line_matches("Under", "1.5", 1)
    assert not over_line_matches("Over 1.5", "2.5", 1)
    assert not over_line_matches("Over 2.5", "1.5", 1)


def test_quote_rejects_snapshot_from_a_previous_score() -> None:
    from predictor.services.advisor.candidates import quote_for_period
    from predictor.services.advisor.domain import Period
    from predictor.services.advisor.sql import OverUnderRow

    stale = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=0,
    )
    assert (
        quote_for_period(
            (stale,),
            period=Period.REGULATION,
            goals_home=0,
            goals_away=1,
            extratime_home=None,
            extratime_away=None,
        )
        is None
    )
    assert (
        playable_candidate(
            _match(),
            (stale,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )
    fresh = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    quote = quote_for_period(
        (fresh,),
        period=Period.REGULATION,
        goals_home=0,
        goals_away=1,
        extratime_home=None,
        extratime_away=None,
    )
    assert quote is not None
    assert quote.odd == Decimal("1.70")


def test_window_waits_for_a_stable_set_and_fires_once() -> None:
    window = CandidateWindow()
    signature = ((7, "regulation", 0, 1, 2),)
    assert window.ready(signature, NOW, timedelta(seconds=20)) is False
    assert (
        window.ready(signature, NOW + timedelta(seconds=19), timedelta(seconds=20))
        is False
    )
    assert (
        window.ready(signature, NOW + timedelta(seconds=20), timedelta(seconds=20))
        is True
    )
    assert (
        window.ready(signature, NOW + timedelta(seconds=21), timedelta(seconds=20))
        is True
    )
    window.mark_fired()
    assert (
        window.ready(signature, NOW + timedelta(seconds=40), timedelta(seconds=20))
        is False
    )
    window.hold_for_retry(NOW + timedelta(seconds=40))
    assert (
        window.ready(signature, NOW + timedelta(seconds=50), timedelta(seconds=20))
        is False
    )
    assert (
        window.ready(signature, NOW + timedelta(seconds=60), timedelta(seconds=20))
        is True
    )
    assert window.next_sleep(NOW, timedelta(seconds=20), 15) == 15
    fresh = CandidateWindow()
    fresh.ready(signature, NOW, timedelta(seconds=20))
    assert fresh.next_sleep(NOW + timedelta(seconds=15), timedelta(seconds=20), 15) == 5
    assert window.ready((), NOW + timedelta(seconds=41), timedelta(seconds=20)) is False


def test_playable_candidate_uses_over_n_plus_half() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    row = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    candidate = playable_candidate(
        _match(),
        (row,),
        limits=_limits(),
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
        for_llm=True,
    )
    assert candidate is not None
    assert candidate.n == 1
    assert candidate.odd == Decimal("1.70")
    assert candidate.ordinal == 2
    assert (
        playable_candidate(
            _match(),
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset({(7, "regulation", 0, 1, 2)}),
            for_llm=True,
        )
        is None
    )
    stale = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=301),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            _match(),
            (stale,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )


def test_llm_playable_requires_second_half_open_line_and_min_odd() -> None:
    from predictor.services.advisor.domain import EXTRA_TIME_OVER_UNDER
    from predictor.services.advisor.sql import OverUnderRow

    # Minute 60: G3 (short_late) stays off so MIN_ANALYSIS_ODD remains playable.
    early = _match(elapsed=60)
    row = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=True,
        captured_at=NOW - timedelta(seconds=300),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is not None
    )
    et = _match(
        status_short="ET",
        elapsed=105,
        goals_home=1,
        goals_away=0,
        extratime_home=1,
        extratime_away=0,
    )
    et_row = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        market=EXTRA_TIME_OVER_UNDER,
        home_goals=1,
        away_goals=0,
    )
    et_candidate = playable_candidate(
        et,
        (et_row,),
        limits=_limits(),
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
        for_llm=True,
    )
    assert et_candidate is not None
    assert et_candidate.period == "extra_time"
    assert et_candidate.n == 1
    first_half = _match(status_short="1H", elapsed=40)
    assert (
        playable_candidate(
            first_half,
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )
    fresh = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            first_half,
            (fresh,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=False,
        )
        is not None
    )
    late = _match(elapsed=80, extra=1)
    assert (
        playable_candidate(
            late,
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )
    below = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD - Decimal("0.001"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (below,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )
    suspended = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=False,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (suspended,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )


def test_playable_candidate_starts_analysis_from_min_analysis_odd() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    # Minute 60 keeps G3 (short_late) off so the analysis floor stays visible.
    early = _match(elapsed=60)
    priced = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (priced,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is not None
    )
    below = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD - Decimal("0.001"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (below,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )


def test_baseline_playable_uses_strategy_min_combined_odd() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    early = _match(elapsed=60, status_short="1H")
    floor = Decimal("1.600")
    short = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.500"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    priced = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=floor,
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            early,
            (short,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=False,
            min_odd=floor,
        )
        is None
    )
    assert (
        playable_candidate(
            early,
            (priced,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=False,
            min_odd=floor,
        )
        is not None
    )
    # LLM analysis may still see 1.50 in 2H; partners cover the coupon floor.
    late = _match(elapsed=60)
    assert (
        playable_candidate(
            late,
            (short,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is not None
    )


def test_prompt_gate_blocks_llm_but_not_baseline() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    row = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="3.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=2,
        away_goals=1,
    )
    high = _match(goals_home=2, goals_away=1)
    assert (
        playable_candidate(
            high,
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=False,
        )
        is not None
    )
    assert (
        playable_candidate(
            high,
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )
    short = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.190"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=1,
    )
    assert (
        playable_candidate(
            _match(elapsed=61),
            (short,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
            for_llm=True,
        )
        is None
    )


def test_lone_candidate_allowed_needs_strong_regulation_single() -> None:
    weak = CycleCandidate(
        fixture_id=1,
        period="regulation",
        goals_home=0,
        goals_away=1,
        ordinal=2,
        candidate_rule="favorite_losing",
        odd=Decimal("1.79"),
        n=1,
    )
    strong = CycleCandidate(
        fixture_id=1,
        period="regulation",
        goals_home=0,
        goals_away=1,
        ordinal=2,
        candidate_rule="favorite_losing",
        odd=Decimal("1.80"),
        n=1,
    )
    et = CycleCandidate(
        fixture_id=2,
        period="extra_time",
        goals_home=1,
        goals_away=1,
        ordinal=1,
        candidate_rule="favorite_losing",
        odd=Decimal("2.50"),
        n=0,
    )
    floor = Decimal("1.600")
    assert lone_candidate_allowed((), min_combined_odd=floor) is True
    assert lone_candidate_allowed((strong,), min_combined_odd=floor) is True
    assert lone_candidate_allowed((weak,), min_combined_odd=floor) is False
    assert lone_candidate_allowed((et,), min_combined_odd=floor) is False
    assert lone_candidate_allowed((weak, et), min_combined_odd=floor) is True
    high_floor = Decimal("1.900")
    assert lone_candidate_allowed((strong,), min_combined_odd=high_floor) is False
    assert (
        lone_candidate_allowed(
            (
                CycleCandidate(
                    fixture_id=1,
                    period="regulation",
                    goals_home=0,
                    goals_away=1,
                    ordinal=2,
                    candidate_rule="favorite_losing",
                    odd=Decimal("1.900"),
                    n=1,
                ),
            ),
            min_combined_odd=high_floor,
        )
        is True
    )


def test_cycle_prompt_p6_prices_candidates_and_forbids_padding() -> None:
    candidates = (
        CycleCandidate(
            fixture_id=7,
            period="regulation",
            goals_home=0,
            goals_away=1,
            ordinal=2,
            n=1,
            odd=Decimal("1.300"),
            candidate_rule="favorite_losing",
        ),
        CycleCandidate(
            fixture_id=8,
            period="extra_time",
            goals_home=1,
            goals_away=1,
            ordinal=1,
            n=0,
            odd=Decimal("1.400"),
            candidate_rule="tie_deficit",
        ),
    )
    prompt, body = render_cycle_prompt(
        candidates,
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        match_details={
            7: {
                "stats": [{"label": "Strzały", "home": "8", "away": "3"}],
                "events": [],
            }
        },
        first_odds={7: FirstOver(odd=Decimal("1.550"), minute=46)},
    )
    assert "min_analysis_odd" not in body
    assert body["min_combined_odd"] == "1.600"
    assert body["max_legs"] == 3
    first = body["candidates"][0]
    assert first["period"] == "regulation"
    assert "ordinal" not in first
    assert "odd" not in first
    assert first["price"] == {
        "odd": "1.300",
        "implied_pct": "77",
        "breakeven_single_pct": "87",
        "breakeven_pair_leg_pct": "82",
        "first_odd": "1.550",
        "first_odd_minute": 46,
    }
    assert first["detail"]["stats"][0]["label"] == "Strzały"
    second = body["candidates"][1]
    assert second["period"] == "extra_time"
    assert second["n"] == 0
    assert second["detail"] is None
    assert "first_odd" not in second["price"]
    assert PROMPT_VERSION == "p6"
    assert "below 1.50" in prompt
    assert "1.80 or higher" in prompt
    assert "never a favourite code" in prompt
    assert "rather than a very short favourite" not in prompt
    assert "already 0–1 with minute at most 60" in prompt
    assert "Prefer coupons" not in prompt
    assert "risk filter over the baseline rule" in prompt
    assert f"x {PAYOUT_KEEP:.2f}" in prompt
    assert "Never add a leg only to reach the floor; use wait instead." in prompt
    assert "at least 1.60" in prompt
    assert "coupon of 1 to 3 fixture ids" in prompt
    assert "odd >= 1.20" in prompt
    assert "at most 2 goals in the settlement period" in prompt
    assert "priced under 2.00" in prompt
    assert "no odd under 1.20 after minute 60" in prompt
    assert "odd of at least 1.80" in prompt
    assert "300 seconds old" in prompt
    assert "For period = regulation only: Inside the regulation gate" in prompt
    assert "There is no track record for extra time" in prompt
    assert "stats_available is false" in prompt
    assert "rise from price.first_odd to price.odd is not a signal" in prompt
    assert "reject means the state itself is bad" in prompt
    assert "comparison labels are Polish" in prompt
    assert "matching the response schema" in prompt
    assert prompt.endswith(json.dumps(body, ensure_ascii=True))


def test_price_block_uses_payout_keep_break_evens() -> None:
    unit = Decimal("1")
    for odd, first in (
        (Decimal("1.300"), None),
        (Decimal("1.600"), None),
        (Decimal("2.000"), FirstOver(odd=Decimal("1.900"), minute=50)),
    ):
        block = _price(odd, first)
        assert block["odd"] == str(odd)
        assert block["implied_pct"] == str(
            (Decimal("100") / odd).quantize(unit, rounding=ROUND_HALF_UP)
        )
        assert block["breakeven_single_pct"] == str(
            (Decimal("100") / (odd * PAYOUT_KEEP)).quantize(
                unit, rounding=ROUND_HALF_UP
            )
        )
        pair_factor = (Decimal(1) / PAYOUT_KEEP).sqrt()
        assert block["breakeven_pair_leg_pct"] == str(
            ((Decimal("100") * pair_factor) / odd).quantize(
                unit, rounding=ROUND_HALF_UP
            )
        )
        if first is None:
            assert "first_odd" not in block
        else:
            assert block["first_odd"] == str(first.odd)
            assert block["first_odd_minute"] == first.minute
    lone = _price(Decimal("1.500"), FirstOver(odd=Decimal("1.450"), minute=None))
    assert lone["first_odd"] == "1.450"
    assert "first_odd_minute" not in lone
    assert _price(Decimal("1.300")) == {
        "odd": "1.300",
        "implied_pct": "77",
        "breakeven_single_pct": "87",
        "breakeven_pair_leg_pct": "82",
    }


def test_candidate_body_odd_reads_price_with_flat_fallback() -> None:
    assert _candidate_body_odd({"price": {"odd": "1.75"}}) == Decimal("1.75")
    assert _candidate_body_odd({"odd": "1.40"}) == Decimal("1.40")


def test_odds_refresh_only_scopes_gated_fixtures() -> None:
    from predictor.services.advisor.candidates import fixtures_needing_odds_refresh
    from predictor.services.advisor.sql import OverUnderRow

    gated = _match(fixture_id=880_764)
    stale = OverUnderRow(
        fixture_id=880_764,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.850"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
        home_goals=0,
        away_goals=0,
    )
    assert fixtures_needing_odds_refresh(
        (gated,),
        {880_764: (stale,)},
        only=frozenset({880_764}),
    ) == (880_764,)
    assert (
        fixtures_needing_odds_refresh(
            (gated,),
            {880_764: (stale,)},
            only=frozenset({880_999}),
        )
        == ()
    )
