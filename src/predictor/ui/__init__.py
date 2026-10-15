"""Status-process HTML. No ETL, no API-Football, no writer lock."""

from predictor.ui.live_board import (
    render_bets_html,
    render_day_html,
    render_live_html,
)
from predictor.ui.match_detail import render_match_html, render_match_missing

__all__ = [
    "render_bets_html",
    "render_day_html",
    "render_live_html",
    "render_match_html",
    "render_match_missing",
]
