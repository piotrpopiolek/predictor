"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from pathlib import Path

import pytest

from predictor.services.advisor.browse import (
    BrowseError,
    clamp_limit,
)


def test_unknown_table_is_not_found() -> None:
    with pytest.raises(BrowseError) as raised:
        clamp_limit(0)
    assert raised.value.code == "bad_limit"
    assert clamp_limit(10000) == 100
    assert clamp_limit(None) == 50


def test_browse_module_has_no_writes() -> None:
    text = Path("src/predictor/services/advisor/browse.py").read_text(encoding="utf-8")
    for banned in (
        "session.add",
        "session.delete",
        "session.execute(delete",
        "session.execute(update",
        "session.execute(insert",
        ".commit(",
        "on_conflict",
    ):
        assert banned not in text
