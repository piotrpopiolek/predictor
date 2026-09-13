"""Pydantic models for /predictions. Extra fields kept for FR-006."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from predictor.schemas.catalog import ExtraAllow
from predictor.schemas.fixtures import FixtureItem


class PredictionWinner(ExtraAllow):
    id: int | None = None
    name: str | None = None
    comment: str | None = None


class PredictionGoals(ExtraAllow):
    home: str | int | float | None = None
    away: str | int | float | None = None


class PredictionPercent(ExtraAllow):
    home: str | None = None
    draw: str | None = None
    away: str | None = None


class PredictionsBlock(ExtraAllow):
    winner: PredictionWinner | None = None
    win_or_draw: bool | None = None
    under_over: str | None = None
    goals: PredictionGoals | None = None
    advice: str | None = None
    percent: PredictionPercent | None = None


class PredictionItem(ExtraAllow):
    predictions: PredictionsBlock | None = None
    comparison: dict[str, Any] | None = None
    teams: dict[str, Any] | None = None
    h2h: list[FixtureItem] = Field(default_factory=list)
