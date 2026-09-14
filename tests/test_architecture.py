from __future__ import annotations

from pathlib import Path

from predictor.constants import WRITER_LOCK_KEY
from predictor.services.health import current_alembic_head
from predictor.services.lock import advisory_lock_parts, lock_busy_message
from predictor.services.metrics import metrics_token_ok, render_metrics


def test_writer_lock_key_is_documented_constant() -> None:
    assert WRITER_LOCK_KEY == 739201


def test_only_lock_module_calls_try_advisory_lock() -> None:
    root = Path("src/predictor")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "pg_try_advisory_xact_lock" in text:
            offenders.append(str(path))
        if "pg_try_advisory_lock" in text and path.name != "lock.py":
            offenders.append(str(path))
    assert offenders == []


def test_api_does_not_import_football_client() -> None:
    source = Path("src/predictor/api/main.py").read_text(encoding="utf-8")
    board = Path("src/predictor/services/live_board.py").read_text(encoding="utf-8")
    assert "FootballClient" not in source
    assert "httpx" not in source
    assert "pg_try_advisory_lock" not in source
    assert "FootballClient" not in board
    assert "httpx" not in board


def test_catalog_ingest_does_not_call_fixtures() -> None:
    text = Path("src/predictor/services/ingest/catalog.py").read_text(encoding="utf-8")
    assert '"/fixtures"' not in text
    assert "'/fixtures'" not in text


def test_enrichment_uses_id_not_ids_batch() -> None:
    for relative in (
        "src/predictor/services/ingest/enrichment.py",
        "src/predictor/services/ingest/persist_children.py",
        "src/predictor/services/ingest/prematch.py",
        "src/predictor/services/ingest/persist_odds.py",
        "src/predictor/services/ingest/persist_predictions.py",
        "src/predictor/services/ingest/global_entities.py",
        "src/predictor/services/ingest/persist_seasonal.py",
        "src/predictor/services/ingest/persist_people.py",
        "src/predictor/services/queue.py",
        "src/predictor/services/completeness.py",
        "src/predictor/worker/main.py",
    ):
        text = Path(relative).read_text(encoding="utf-8")
        assert '"ids"' not in text
        assert "'ids'" not in text
    worker = Path("src/predictor/worker/main.py").read_text(encoding="utf-8")
    assert "refresh_finalization" in worker
    assert "refresh_pending" in worker
    assert "PrematchIngest" in worker
    assert "GlobalIngest" in worker
    assert "priority_eight" in worker
    assert worker.index("prematch.refresh_pending") < worker.index(
        "global_ingest.refresh_pending"
    )


def test_global_catalog_skips_lookups_and_uses_coachs() -> None:
    ingest = Path("src/predictor/services/ingest/global_entities.py").read_text(
        encoding="utf-8"
    )
    queue = Path("src/predictor/services/queue.py").read_text(encoding="utf-8")
    worker = Path("src/predictor/worker/main.py").read_text(encoding="utf-8")
    assert "/teams/seasons" not in ingest
    assert "/players/seasons" not in ingest
    assert "/teams/seasons" not in queue
    assert "/players/seasons" not in queue
    assert "/teams/seasons" not in worker
    assert '"/coachs"' in ingest
    assert '"/coaches"' not in ingest
    assert '"/coachs"' in queue
    assert "LOOKUP_WITHOUT_HTTP" in ingest
    assert "TEAM_STATS_SENTINEL_DATE" in ingest


def test_prematch_odds_use_odds_bets_not_live() -> None:
    persist = Path("src/predictor/services/ingest/persist_odds.py").read_text(
        encoding="utf-8"
    )
    ingest = Path("src/predictor/services/ingest/prematch.py").read_text(
        encoding="utf-8"
    )
    assert "odds_bets" in persist or "OddsBet" in persist
    assert "OddsLiveBet" not in persist
    assert "FixtureOddsLive" not in persist
    assert "odds_live_bets" not in persist
    assert "OddsLiveBet" not in ingest
    assert "FixtureOddsLive" not in ingest
    assert '"/odds/live"' not in ingest


def test_live_odds_persist_is_append_only() -> None:
    text = Path("src/predictor/services/ingest/persist_live.py").read_text(
        encoding="utf-8"
    )
    assert "on_conflict_do_update" not in text
    assert "bookmaker" not in text.casefold()


def test_no_vendor_api_host_in_application() -> None:
    root = Path("src")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "api-sports.io" not in text
        assert "api-football.com" not in text


def test_alembic_head_helper_matches_w1() -> None:
    assert current_alembic_head() == "0017_etl_state"


def test_advisory_lock_parts_split_bigint() -> None:
    assert advisory_lock_parts(WRITER_LOCK_KEY) == (0, WRITER_LOCK_KEY)


def test_lock_busy_message_without_holder() -> None:
    message = lock_busy_message(None)
    assert str(WRITER_LOCK_KEY) in message
    assert "another backend" in message


def test_src_has_no_etl_lease() -> None:
    root = Path("src/predictor")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "etl_lease" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert offenders == []


def test_observability_compose_has_no_vendor_api_host() -> None:
    for relative in (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.observability.yml",
        "deploy/otel/otel-collector.yml",
        "deploy/prometheus/prometheus.yml",
        "deploy/prometheus/alerts.yml",
    ):
        text = Path(relative).read_text(encoding="utf-8").lower()
        assert "api-sports.io" not in text
        assert "api-football.com" not in text


def test_metrics_token_and_labels() -> None:
    assert metrics_token_ok(None, "secret") is False
    assert metrics_token_ok("Bearer secret", "secret") is True
    assert metrics_token_ok("Bearer other", "secret") is False
    body = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={"pending": 2},
    )
    assert 'service="status"' in body
    assert 'environment="local"' in body
    assert "predictor_writer_lock" in body
    assert "predictor_live_snapshot_age_seconds" in body
    assert "predictor_quota_used" in body
    assert "predictor_live_poll_interval_seconds" in body
    assert "predictor_quota_seconds_until_reset" in body
    body_q = render_metrics(
        environment="local",
        lock_held=True,
        task_counts={"pending": 2},
        queue_counts=(("/teams", "pending", 5),),
    )
    assert "predictor_etl_queue" in body_q
    assert 'endpoint="/teams"' in body_q
    assert "fixture_id" not in body
    assert "fixture_id" not in body_q
    assert "task_id" not in body
    assert "run_id" not in body
