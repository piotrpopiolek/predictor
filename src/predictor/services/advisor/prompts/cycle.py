"""Prompt for one sandbox cycle. Version p5: risk filter over baseline.

Odds are context, not an output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from predictor.constants import PAYOUT_KEEP
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import (
    LLM_MAX_FAVORITE_ODD,
    LLM_MAX_PERIOD_GOALS,
    LLM_SHORT_ODD,
    LLM_SHORT_ODD_AFTER_MINUTE,
    LLM_SINGLE_MIN_ODD,
    MIN_ANALYSIS_ODD,
    QUOTE_MAX_AGE,
)

PROMPT_VERSION = "p5"

# Regulation-only directions inside the A0 gate (B1). Extra time has no
# track record yet — the prompt states that separately. Exact rates stay out
# of the prompt; refresh via `.ai/b1_state_priors.sql` on predictor_dev when
# the finished sample is large enough to revisit.
_STATE_PRIORS = (
    "Inside the regulation gate, historical edge leans toward total goals "
    "already 0–1 with minute at most 60, and toward a pre-match favourite "
    "priced 1.50–2.00 rather than a very short favourite below 1.50."
)

_PCT = Decimal("1")
_HUNDRED = Decimal("100")


class _FirstOdd(Protocol):
    odd: Decimal
    minute: int | None


def _odd_text(value: Decimal) -> str:
    return f"{value:.2f}"


def _price(odd: Decimal, first: _FirstOdd | None = None) -> dict[str, str | int]:
    """Break-even percentages after tax, plus optional first same-score Over."""
    implied = (_HUNDRED / odd).quantize(_PCT, rounding=ROUND_HALF_UP)
    breakeven_single = (_HUNDRED / (odd * PAYOUT_KEEP)).quantize(
        _PCT, rounding=ROUND_HALF_UP
    )
    pair_factor = (Decimal(1) / PAYOUT_KEEP).sqrt()
    breakeven_pair = ((_HUNDRED * pair_factor) / odd).quantize(
        _PCT, rounding=ROUND_HALF_UP
    )
    out: dict[str, str | int] = {
        "odd": str(odd),
        "implied_pct": str(implied),
        "breakeven_single_pct": str(breakeven_single),
        "breakeven_pair_leg_pct": str(breakeven_pair),
    }
    if first is not None:
        out["first_odd"] = str(first.odd)
        if first.minute is not None:
            out["first_odd_minute"] = int(first.minute)
    return out


def _instructions(*, min_combined_odd: Decimal, max_legs: int) -> str:
    keep = _odd_text(PAYOUT_KEEP)
    floor = _odd_text(min_combined_odd)
    quote_seconds = int(QUOTE_MAX_AGE.total_seconds())
    return " ".join(
        (
            "You are a risk filter over the baseline rule. Every candidate is "
            "a match in the second half or in extra time where the pre-match "
            "favourite trails by exactly one goal (or by one goal on aggregate "
            "for candidate_rule tie_deficit). The bet is Over N+0.5 for the "
            "candidate's period: one more goal by either team in that period. "
            "Baseline bets all of them. Keep the states that make money and "
            "drop the ones that lose it. Protecting the bankroll matters more "
            "than volume: when evidence is thin, do not bet.",
            f"A coupon pays stake x product of leg odds x {keep}. "
            "Each candidate.price gives implied_pct, breakeven_single_pct and "
            "breakeven_pair_leg_pct. Bet a leg only when your "
            "estimated_probability, as a percent, is at least the break-even "
            "of the coupon you put it in.",
            "Start from implied_pct and move away from it only with evidence "
            "present in the input. For period = regulation only: "
            f"{_STATE_PRIORS}",
            "period = extra_time settles on extra-time goals only: n counts "
            "goals scored in extra time, goals from the 90 minutes do not "
            "count, and penalties never count. There is no track record for "
            "extra time in this sandbox, so the regulation priors do not "
            "apply; bet only with clear evidence and prefer reject or wait "
            "otherwise. minutes_left counts to minute 120.",
            f"The default coupon is a pair: the {keep} cut is taken once per "
            "coupon, so a leg in a pair only needs breakeven_pair_leg_pct "
            "while a single needs breakeven_single_pct. Any two candidates "
            "here clear the combined-odd floor together. Add a third leg only "
            "when it clears its own break-even. Never add a leg only to reach "
            "the floor; use wait instead. A single is allowed only when this "
            "cycle has exactly one candidate, and only when "
            f"estimated_probability x odd x {keep} is at least 1. A single in "
            "a cycle with two or more candidates, or a single below that "
            "value, rejects the whole reply. When one of two candidates is "
            "weak, do not bet the other alone: wait or reject. A coupon is "
            "accepted only when the product of its database odds is at least "
            f"{floor}; legs from different periods may share a coupon.",
            "Candidates were already filtered: at most "
            f"{LLM_MAX_PERIOD_GOALS} goals in the settlement period, a "
            "pre-match favourite priced under "
            f"{_odd_text(LLM_MAX_FAVORITE_ODD)}, no odd under "
            f"{_odd_text(LLM_SHORT_ODD)} after minute "
            f"{LLM_SHORT_ODD_AFTER_MINUTE} in regulation, and a lone candidate "
            "only when it is in regulation with an odd of at least "
            f"{_odd_text(LLM_SINGLE_MIN_ODD)}. Each one is already 2H or extra "
            "time (ET/BT), within the strategy minute cutoff for its period, "
            "with an open Over N+0.5 line for that period at odd >= "
            f"{_odd_text(MIN_ANALYSIS_ODD)} from a snapshot at most "
            f"{quote_seconds} seconds old.",
            "When stats_available is false, do not use high_shot_volume or "
            "low_tempo, and do not guess numbers that are absent. In extra "
            "time, stats cover the whole match, not only extra time.",
            "The Over price rises on its own while no goal is scored, so a "
            "rise from price.first_odd to price.odd is not a signal. A price "
            "that stayed flat or fell despite time passing, or next_goal_odds "
            "clearly favouring the favourite, is.",
            "Every reason code must follow from a field present in the input: "
            "high_shot_volume and low_tempo need stats, red_card needs "
            "home_reds or away_reds above 0, and weak_favorite means a "
            "favourite priced near the "
            f"{_odd_text(LLM_MAX_FAVORITE_ODD)} limit, not a missing one.",
            "reject means the state itself is bad and stays rejected for this "
            "score and period. wait means the state is fine but this cycle "
            "has no valid coupon for it. A wait in regulation does not carry "
            "into extra time.",
            "Each candidate.detail is compact match context: minute, "
            "minutes_left, favorite (side and prematch_odd), next_goal_odds, "
            "last_goal and minutes_since_last_goal, Goal events, et_home and "
            "et_away in extra time, stats when available, reds, team goal "
            "form, lineup strength, attack subs, plus the pre-match "
            "predictions advice, expected_home, expected_away, pct_draw and "
            "comparison. Those predictions describe the match before kickoff, "
            "and the comparison labels are Polish (Atak, Obrona, H2H, Forma, "
            "Gole, Ogółem, Poisson).",
            "Reply with JSON only, no markdown, matching the response schema. "
            "Field types, ranges, and reason codes are in that schema. Do not "
            "add fields and do not send an odd. Decide each listed fixture_id "
            "exactly once. A bet must appear in exactly one coupon of 1 to "
            f"{max_legs} fixture ids.",
        )
    )


def render_cycle_prompt(
    candidates: Sequence[CycleCandidate],
    *,
    min_combined_odd: Decimal,
    max_legs: int,
    match_details: Mapping[int, dict[str, Any]] | None = None,
    first_odds: Mapping[int, _FirstOdd] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the prompt and the JSON stored on the cycle."""
    details = {} if match_details is None else match_details
    first = {} if first_odds is None else first_odds
    body = {
        "candidates": [
            {
                "fixture_id": item.fixture_id,
                "period": item.period,
                "goals_home": item.goals_home,
                "goals_away": item.goals_away,
                "n": item.n,
                "candidate_rule": item.candidate_rule,
                "price": _price(item.odd, first.get(item.fixture_id)),
                "detail": details.get(item.fixture_id),
            }
            for item in candidates
        ],
        "min_combined_odd": str(min_combined_odd),
        "max_legs": max_legs,
    }
    prompt = (
        _instructions(min_combined_odd=min_combined_odd, max_legs=max_legs)
        + f" Candidates: {json.dumps(body, ensure_ascii=True)}"
    )
    return prompt, body
