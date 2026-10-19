"""Prompt for one sandbox cycle. Version p3. Odds are context, not an output."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import MIN_ANALYSIS_ODD

PROMPT_VERSION = "p3"

_REASONS = (
    "favorite_pressure",
    "high_shot_volume",
    "low_tempo",
    "red_card",
    "odd_too_low",
    "late_minute",
    "weak_favorite",
)


def render_cycle_prompt(
    candidates: Sequence[CycleCandidate],
    *,
    min_combined_odd: Decimal,
    max_legs: int,
    match_details: Mapping[int, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the prompt and the JSON stored on the cycle."""
    details = {} if match_details is None else match_details
    body = {
        "candidates": [
            {
                "fixture_id": item.fixture_id,
                "period": item.period,
                "goals_home": item.goals_home,
                "goals_away": item.goals_away,
                "ordinal": item.ordinal,
                "n": item.n,
                "odd": str(item.odd),
                "candidate_rule": item.candidate_rule,
                "detail": details.get(item.fixture_id),
            }
            for item in candidates
        ],
        "min_analysis_odd": str(MIN_ANALYSIS_ODD),
        "min_combined_odd": str(min_combined_odd),
        "max_legs": max_legs,
    }
    prompt = (
        "You decide a next-goal Over bet for every candidate below. "
        "Reply with JSON only, no markdown. "
        "Schema: candidates[{fixture_id, decision, estimated_probability, "
        "confidence, reasons, rationale}] and coupons[{fixture_ids}]. "
        "decision is bet, reject, or wait. "
        f"reasons use only {_REASONS}. "
        "Do not add fields and do not send an odd. "
        "A bet must appear in exactly one coupon of 1 to "
        f"{max_legs} fixture ids. "
        f"Each listed candidate already has odd >= {MIN_ANALYSIS_ODD}. "
        f"A coupon is accepted only when the product of its odds is "
        f">= {min_combined_odd}. Prefer coupons of 2–{max_legs} legs so "
        "the combined odd clears that floor; use wait when more priced "
        "candidates are needed. "
        "Each candidate.detail is compact match context: score/clock, "
        "Goal/Card events, FT stats, lineup strength, attack subs, "
        "goal price, prematch/next-goal odds, team goal form, and "
        "prediction comparison. Use it for the decision. "
        f"Candidates: {json.dumps(body, ensure_ascii=True)}"
    )
    return prompt, body
