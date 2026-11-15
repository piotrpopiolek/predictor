"""Two decide/validate passes on a frozen set. Not a bet-quality score.

The command calls the model. Tests call ``score_replies`` with canned text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from predictor.services.advisor.llm.validate import (
    PricedCandidate,
    Verdict,
    review_model_output,
)

_CASES = Path(__file__).with_name("stability_cases.json")


@dataclass(frozen=True, slots=True)
class FrozenCase:
    name: str
    prompt: str
    min_odd: Decimal
    max_legs: int
    candidates: tuple[PricedCandidate, ...]


@dataclass(frozen=True, slots=True)
class StabilityReport:
    cases: int
    agreed: int
    validation_failures: tuple[int, int]

    def render(self) -> str:
        first, second = self.validation_failures
        return (
            f"cases: {self.cases}\n"
            f"agreed: {self.agreed}\n"
            f"validation_failures_run_1: {first}\n"
            f"validation_failures_run_2: {second}\n"
        )


def load_cases(path: Path = _CASES) -> tuple[FrozenCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[FrozenCase] = []
    for item in raw:
        cases.append(
            FrozenCase(
                name=str(item["name"]),
                prompt=str(item["prompt"]),
                min_odd=Decimal(str(item["min_odd"])),
                max_legs=int(item["max_legs"]),
                candidates=tuple(
                    PricedCandidate(int(row["fixture_id"]), Decimal(str(row["odd"])))
                    for row in item["candidates"]
                ),
            )
        )
    return tuple(cases)


def score_replies(
    cases: tuple[FrozenCase, ...],
    replies: tuple[tuple[str | None, str | None], ...],
) -> StabilityReport:
    """Count agreement and validation rejects. A missing reply is not a reject."""
    if len(cases) != len(replies):
        raise ValueError("one reply pair per case")
    failures = [0, 0]
    agreed = 0
    for case, pair in zip(cases, replies, strict=True):
        labels: list[str | None] = []
        for index, text in enumerate(pair):
            if text is None:
                labels.append(None)
                continue
            verdict = _review(case, text)
            if verdict.status != "ok":
                failures[index] += 1
                labels.append(None)
                continue
            labels.append(_label(verdict))
        if labels[0] is not None and labels[0] == labels[1]:
            agreed += 1
    return StabilityReport(len(cases), agreed, (failures[0], failures[1]))


def _review(case: FrozenCase, text: str) -> Verdict:
    return review_model_output(
        text,
        case.candidates,
        set(),
        min_odd=case.min_odd,
        max_legs=case.max_legs,
    )


def _label(verdict: Verdict) -> str:
    if verdict.decision is None:
        return "reject"
    kinds = {item.decision for item in verdict.decision.candidates}
    if "bet" in kinds:
        return "bet"
    if "wait" in kinds:
        return "wait"
    return "reject"
