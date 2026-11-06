"""Prompt for one sandbox cycle. Version p4. Odds are context, not an output."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from predictor.constants import PAYOUT_KEEP
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import MIN_ANALYSIS_ODD

PROMPT_VERSION = "p4"

_PCT = Decimal("1")
_HUNDRED = Decimal("100")


class _FirstOdd(Protocol):
    odd: Decimal
    minute: int | None


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
        "You decide a next-goal Over bet for every candidate below. "
        "Reply with JSON only, no markdown, matching the response schema. "
        "Field types, ranges, and reason codes are in that schema. "
        "Do not add fields and do not send an odd. "
        "Decide each listed fixture_id exactly once. "
        "A bet must appear in exactly one coupon of 1 to "
        f"{max_legs} fixture ids. "
        "Each listed candidate is already 2H or extra time (ET/BT), "
        "minute at most the strategy cutoff for that period, with an open "
        "Over N+0.5 line for the settlement period "
        "(N = goals in that period) "
        f"and odd >= {MIN_ANALYSIS_ODD} from a snapshot at most 300 seconds old. "
        f"A coupon is accepted only when the product of its database odds is "
        f">= {min_combined_odd}. Prefer coupons of 2–{max_legs} legs so "
        "the combined odd clears that floor. "
        "Use wait, not bet, when the coupon cannot clear that floor. "
        "candidate.price carries the live Over odd, implied chance, and "
        f"break-even percentages after the {PAYOUT_KEEP} payout keep "
        "(single and pair-leg). first_odd is the earliest same-score Over "
        "in this period when available. "
        "Each candidate.detail is compact match context: score/clock, "
        "Goal/Card events, FT stats, lineup strength, attack subs, "
        "goal price, prematch/next-goal odds, team goal form, and "
        "prediction comparison. Use it for the decision. "
        f"Candidates: {json.dumps(body, ensure_ascii=True)}"
    )
    return prompt, body
