"""Targeted live score and odds pull for advisor refresh requests.

Called by the worker under the lock it already holds. Does not acquire a lock.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.logutil import log_json
from predictor.schemas.live import OddsLiveItem
from predictor.services.ingest.fixtures import parse_fixtures
from predictor.services.ingest.odds_live_parse import parse_odds_live_items
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.persist_live import (
    mapped_next_goal_ids,
    persist_odds_live_snapshots,
)

_SERVICE = "worker"


async def persist_targeted_live(
    client: FootballClient,
    session_factory: async_sessionmaker[AsyncSession],
    fixture_ids: Sequence[int],
    *,
    now: datetime | None = None,
) -> bool:
    """Fetch /fixtures?id= and /odds/live?fixture=; persist. False on API failure."""
    ids = sorted({int(fid) for fid in fixture_ids})
    if not ids:
        return True
    captured_at = now or datetime.now(UTC)
    try:
        fixture_items: list[Any] = []
        odds_items: list[OddsLiveItem] = []
        for fixture_id in ids:
            try:
                rows, _current, _total = await fetch_all_pages(
                    client, "/fixtures", params={"id": fixture_id}
                )
            except AuthBlockedError:
                log_json(
                    logging.WARNING,
                    service=_SERVICE,
                    event="candidate_refresh_auth_blocked",
                    fixture_id=fixture_id,
                )
                return False
            except (QuotaExhaustedError, RetryableHttpError, FootballHttpError) as exc:
                log_json(
                    logging.WARNING,
                    service=_SERVICE,
                    event="candidate_refresh_fixtures_failed",
                    fixture_id=fixture_id,
                    error=type(exc).__name__,
                )
                return False
            fixture_items.extend(rows)
            try:
                odds_rows, _current, _total = await fetch_all_pages(
                    client, "/odds/live", params={"fixture": fixture_id}
                )
            except AuthBlockedError:
                log_json(
                    logging.WARNING,
                    service=_SERVICE,
                    event="candidate_refresh_auth_blocked",
                    fixture_id=fixture_id,
                )
                return False
            except (QuotaExhaustedError, RetryableHttpError, FootballHttpError) as exc:
                log_json(
                    logging.WARNING,
                    service=_SERVICE,
                    event="candidate_refresh_odds_failed",
                    fixture_id=fixture_id,
                    error=type(exc).__name__,
                )
                return False
            odds_items.extend(parse_odds_live_items(odds_rows, service=_SERVICE))
        parsed = parse_fixtures(fixture_items)
        async with session_factory() as session, session.begin():
            if parsed:
                await upsert_fixtures(session, parsed)
            mapped = await mapped_next_goal_ids(session)
            await persist_odds_live_snapshots(
                session,
                odds_items,
                captured_at=captured_at,
                mapped_next_goal=mapped,
                discovered_fixture_ids=set(ids),
            )
        log_json(
            logging.INFO,
            service=_SERVICE,
            event="candidate_refresh_ok",
            fixtures=len(ids),
            odds_items=len(odds_items),
        )
        return True
    except Exception:
        log_json(
            logging.ERROR,
            service=_SERVICE,
            event="candidate_refresh_persist_failed",
            fixtures=len(ids),
        )
        return False
