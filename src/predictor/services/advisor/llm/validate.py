"""Semantic checks after the schema. A failure drops the whole cycle."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from predictor.schemas.advisor import LlmDecision
from predictor.services.advisor.domain import combined_odd
from predictor.services.advisor.llm.parse import InvalidDecision, parse_decision


@dataclass(frozen=True, slots=True)
class PricedCandidate:
    fixture_id: int
    odd: Decimal


@dataclass(frozen=True, slots=True)
class AcceptedCoupon:
    fixture_ids: tuple[int, ...]
    combined_odd: Decimal


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str
    detail: str
    decision: LlmDecision | None
    coupons: tuple[AcceptedCoupon, ...]


def review_model_output(
    raw: str,
    playable: Sequence[PricedCandidate],
    open_fixture_ids: set[int],
    *,
    min_odd: Decimal,
    max_legs: int,
) -> Verdict:
    try:
        parsed = parse_decision(raw)
    except InvalidDecision as exc:
        return _invalid(exc.code)
    detail = _semantics(parsed, playable, open_fixture_ids, min_odd, max_legs)
    if detail is not None:
        # The JSON matches the schema. The status is the rule that failed.
        return Verdict(detail, detail, parsed, ())
    odds = {item.fixture_id: item.odd for item in playable}
    accepted = tuple(
        AcceptedCoupon(
            fixture_ids=tuple(coupon.fixture_ids),
            combined_odd=combined_odd(tuple(odds[fid] for fid in coupon.fixture_ids)),
        )
        for coupon in parsed.coupons
    )
    return Verdict("ok", "", parsed, accepted)


def _semantics(
    parsed: LlmDecision,
    playable: Sequence[PricedCandidate],
    open_fixture_ids: set[int],
    min_odd: Decimal,
    max_legs: int,
) -> str | None:
    priced = {item.fixture_id: item.odd for item in playable}
    seen: set[int] = set()
    for candidate in parsed.candidates:
        if candidate.fixture_id in seen:
            return "duplicate_candidate"
        seen.add(candidate.fixture_id)
    if seen != set(priced):
        return "candidate_set"
    used: set[int] = set()
    for coupon in parsed.coupons:
        ids = coupon.fixture_ids
        if len(ids) != len(set(ids)):
            return "duplicate_leg"
        if len(ids) > max_legs:
            return "too_many_legs"
        for fixture_id in ids:
            if fixture_id not in priced:
                return "unknown_fixture"
            if fixture_id in open_fixture_ids:
                return "open_coupon"
            if fixture_id in used:
                return "duplicate_leg"
            used.add(fixture_id)
        try:
            total = combined_odd(tuple(priced[fixture_id] for fixture_id in ids))
        except ValueError:
            return "invalid_odd"
        if total < min_odd:
            return "below_threshold"
    for candidate in parsed.candidates:
        in_coupon = candidate.fixture_id in used
        if candidate.decision == "bet" and not in_coupon:
            return "bet_not_in_coupon"
        if candidate.decision != "bet" and in_coupon:
            return "not_a_bet"
    return None


def _invalid(detail: str) -> Verdict:
    return Verdict("invalid_output", detail, None, ())
