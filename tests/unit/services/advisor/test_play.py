"""Leg scaling for a played sandbox-llm coupon."""

from decimal import Decimal

import pytest

from predictor.services.advisor.domain import combined_odd
from predictor.services.advisor.play import scale_leg_odds


def test_one_leg_is_the_entered_price() -> None:
    assert scale_leg_odds((Decimal("1.700"),), Decimal("1.850")) == (Decimal("1.850"),)


def test_two_legs_multiply_to_the_entered_price() -> None:
    scaled = scale_leg_odds((Decimal("1.500"), Decimal("1.400")), Decimal("2.400"))
    assert combined_odd(scaled) == Decimal("2.400")
    assert scaled[0] != Decimal("1.500") or scaled[1] != Decimal("1.400")


def test_price_at_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        scale_leg_odds((Decimal("1.700"),), Decimal("1.000"))
