"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from decimal import Decimal

import pytest

from predictor.constants import SANDBOX_BANKROLL
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.sandbox import (
    baseline_slips,
    portfolio_balance,
    reply_slips,
)
from predictor.services.advisor.telegram import (
    sandbox_llm_text,
)


def _leg(
    fixture_id: int,
    odd: str,
    *,
    goals_home: int = 0,
    goals_away: int = 1,
    n: int = 1,
    ordinal: int = 2,
    rule: str = "favorite_losing",
) -> CycleCandidate:
    return CycleCandidate(
        fixture_id=fixture_id,
        period="regulation",
        goals_home=goals_home,
        goals_away=goals_away,
        ordinal=ordinal,
        candidate_rule=rule,
        odd=Decimal(odd),
        n=n,
    )


def test_baseline_slips_have_no_odd_floor() -> None:
    slips = baseline_slips(
        (
            _leg(1, "1.20"),
            _leg(1, "1.90"),
            _leg(2, "1"),
            _leg(3, "1.70"),
        )
    )
    assert [slip.legs[0].fixture_id for slip in slips] == [1, 3]
    assert slips[0].legs[0].odd == Decimal("1.20")


def test_reply_drops_a_coupon_when_a_leg_was_not_stored() -> None:
    first = _leg(1, "1.70")
    second = _leg(2, "1.40")
    assert len(reply_slips(((1, 2),), (first, second), {1, 2})) == 1
    assert reply_slips(((1, 2),), (first, second), {1}) == ()
    assert reply_slips(((),), (first,), {1}) == ()


def test_portfolio_balance_counts_open_stakes_and_settled_results() -> None:
    stake = Decimal("10.00")
    keep = Decimal("0.880")
    assert portfolio_balance(()) == SANDBOX_BANKROLL
    assert portfolio_balance(((stake, Decimal("1.700"), keep, "open"),)) == (
        SANDBOX_BANKROLL - stake
    )
    assert portfolio_balance(
        (
            (stake, Decimal("1.700"), keep, "open"),
            (stake, Decimal("1.700"), keep, "open"),
        )
    ) == (SANDBOX_BANKROLL - stake - stake)
    assert portfolio_balance(((stake, Decimal("2.000"), keep, "won"),)) == (
        SANDBOX_BANKROLL + stake * Decimal("2.000") * keep - stake
    )
    assert portfolio_balance(((stake, Decimal("2.000"), keep, "lost"),)) == (
        SANDBOX_BANKROLL - stake
    )
    assert portfolio_balance(((stake, None, keep, "void"),)) == SANDBOX_BANKROLL
    assert (
        portfolio_balance(((stake, Decimal("1.700"), keep, "proposed"),))
        == SANDBOX_BANKROLL
    )
    with pytest.raises(ValueError, match="no odd"):
        portfolio_balance(((stake, None, keep, "won"),))


def test_sandbox_llm_text_has_legs_and_no_secret() -> None:
    text = sandbox_llm_text(
        coupon_id=124,
        lines=(
            "Mes Kerman – Saipa, 0:1, Over 1.5 @ 1.700",
            "Legia – Raków, 1:2, Over 3.5 @ 1.550",
        ),
        stake="21.00",
        combined="2.635",
    )
    assert "Sandbox-llm gra kupon 124" in text
    assert "Mes Kerman – Saipa, 0:1, Over 1.5 @ 1.700" in text
    assert "Legia – Raków, 1:2, Over 3.5 @ 1.550" in text
    assert "Kurs łączny 2.635" in text
    assert "Stawka 21.00" in text
    assert "bot" not in text.casefold()
    assert "token" not in text.casefold()
