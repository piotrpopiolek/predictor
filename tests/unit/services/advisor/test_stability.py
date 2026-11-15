"""Stability counters from canned replies. The model is not called."""

import json

from predictor.services.advisor.stability import load_cases, score_replies


def _candidate(fixture_id: int, decision: str) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "decision": decision,
        "estimated_probability": "0.75" if decision == "bet" else "0.40",
        "confidence": "0.40",
        "reasons": ["favorite_pressure"],
        "rationale": "chasing" if decision == "bet" else "no chase",
    }


def _reply(fixture_ids: tuple[int, ...], decision: str) -> str:
    coupons = [{"fixture_ids": list(fixture_ids)}] if decision == "bet" else []
    return json.dumps(
        {
            "candidates": [
                _candidate(fixture_id, decision) for fixture_id in fixture_ids
            ],
            "coupons": coupons,
        }
    )


def test_canned_replies_count_agreement_and_validation_rejects() -> None:
    cases = load_cases()
    assert len(cases) == 3
    single, pair, short = cases
    one = (single.candidates[0].fixture_id,)
    both = tuple(item.fixture_id for item in pair.candidates)
    short_id = (short.candidates[0].fixture_id,)
    report = score_replies(
        cases,
        (
            (_reply(one, "bet"), _reply(one, "bet")),
            (_reply(both, "reject"), "not-json"),
            (None, _reply(short_id, "reject")),
        ),
    )
    assert report.cases == 3
    assert report.agreed == 1
    assert report.validation_failures == (0, 1)
    text = report.render()
    assert "agreed: 1" in text
    assert "validation_failures_run_2: 1" in text
