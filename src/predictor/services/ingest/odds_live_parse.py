"""Parse raw /odds/live response rows into OddsLiveItem."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from predictor.logutil import log_json
from predictor.schemas.live import OddsLiveItem
from predictor.services.ingest.drift import warn_model_extra


def parse_odds_live_items(raw: list[Any], *, service: str) -> list[OddsLiveItem]:
    items: list[OddsLiveItem] = []
    for row in raw:
        try:
            item = OddsLiveItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service=service,
                event="catalog_row_invalid",
                endpoint="/odds/live",
            )
            continue
        warn_model_extra("/odds/live", item)
        if item.fixture.status is not None:
            warn_model_extra("/odds/live", item.fixture.status)
        for bet in item.odds:
            warn_model_extra("/odds/live", bet)
            for value in bet.values:
                warn_model_extra("/odds/live", value)
        items.append(item)
    return items
