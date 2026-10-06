from __future__ import annotations

from decimal import Decimal

from predictor.services.odds_parse import parse_odd_value


def test_parse_odd_value_accepts_comma_and_rejects_bad() -> None:
    assert parse_odd_value("1,65") == Decimal("1.65")
    assert parse_odd_value(2.5) == Decimal("2.5")
    assert parse_odd_value(Decimal("1.200")) == Decimal("1.200")
    assert parse_odd_value(None) is None
    assert parse_odd_value("") is None
    assert parse_odd_value("  ") is None
    assert parse_odd_value("not-a-number") is None
    assert parse_odd_value("nan") is None
    assert parse_odd_value("inf") is None
