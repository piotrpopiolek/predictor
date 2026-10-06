"""Shared parsing of bookmaker odd strings to Decimal."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_odd_value(raw: object) -> Decimal | None:
    """Parse odd from API/UI; None if missing, empty, invalid, or non-finite."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value
