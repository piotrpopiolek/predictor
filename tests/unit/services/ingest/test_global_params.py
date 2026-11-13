"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from predictor.client.errors import FootballHttpError, RetryableHttpError
from predictor.constants import (
    TEAM_STATS_SENTINEL_DATE,
)
from predictor.services.ingest.global_entities import (
    _http_error_label,
    _http_params,
    _non_positive_identity,
)
from predictor.services.queue import (
    enqueue_global_for_match,
)


def test_enqueue_global_helper_exports() -> None:
    assert callable(enqueue_global_for_match)
    assert TEAM_STATS_SENTINEL_DATE == "1970-01-01"


def test_http_params_rejects_non_positive_player_id() -> None:
    assert _http_params("/players/profiles", {"player": 0}) is None
    assert _http_params("/players/profiles", {"player": -1}) is None
    assert _http_params("/players/profiles", {"player": 42}) == {"player": 42}
    assert _http_params("/transfers", {"player": "0"}) is None
    assert _http_params("/trophies", {"player": 0, "coach": 9}) == {"coach": 9}


def test_non_positive_identity_is_not_a_contract_error() -> None:
    assert _non_positive_identity("/players/profiles", {"player": 0})
    assert _non_positive_identity("/coachs", {"id": 0})
    assert _non_positive_identity("/trophies", {"coach": 0})
    assert not _non_positive_identity("/trophies", {"player": 0, "coach": 9})
    assert not _non_positive_identity("/players", {"league": 0, "season": 2026})
    assert not _non_positive_identity("/players/profiles", {})


def test_http_error_label_includes_status() -> None:
    assert _http_error_label(FootballHttpError(404, "/players")) == (
        "FootballHttpError: 404"
    )
    assert _http_error_label(RetryableHttpError(None)) == "RetryableHttpError"
    assert _http_error_label(RetryableHttpError(503)) == "RetryableHttpError: 503"
