"""Prompt for one sandbox cycle. Version p1. Odds are context, not an output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from predictor.services.advisor.cycle import CycleCandidate

PROMPT_VERSION = "p1"

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
) -> tuple[str, dict[str, Any]]:
    """Return the prompt and the JSON stored on the cycle."""
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
            }
            for item in candidates
        ]
    }
    prompt = (
        "You decide a next-goal Over bet for every candidate below. "
        "Reply with JSON only, no markdown. "
        "Schema: candidates[{fixture_id, decision, estimated_probability, "
        "confidence, reasons, rationale}] and coupons[{fixture_ids}]. "
        "decision is bet, reject, or wait. "
        f"reasons use only {_REASONS}. "
        "Do not add fields and do not send an odd. "
        "A bet must appear in exactly one coupon of 1 to 3 fixture ids. "
        f"Candidates: {json.dumps(body, ensure_ascii=True)}"
    )
    return prompt, body
