"""W1 schema gate: DDL tables, append-only live PK, ETL state, no etl_lease."""

from __future__ import annotations

import pytest
from psycopg.errors import CheckViolation

from tests.db import app_connect

EXPECTED_TABLES = frozenset(
    {
        "countries",
        "seasons",
        "venues",
        "leagues",
        "league_seasons",
        "league_rounds",
        "fixture_statuses",
        "teams",
        "players",
        "coaches",
        "bookmakers",
        "odds_bets",
        "odds_live_bets",
        "fixtures",
        "fixture_events",
        "fixture_lineups",
        "fixture_lineup_players",
        "fixture_statistics",
        "fixture_player_stats",
        "standings",
        "player_statistics",
        "squad_members",
        "player_career_teams",
        "team_season_statistics",
        "predictions",
        "prediction_h2h",
        "fixture_odds",
        "fixture_odds_live",
        "odds_fixture_mapping",
        "transfers",
        "injuries",
        "sidelined",
        "coach_career",
        "trophies",
        "etl_runs",
        "etl_tasks",
    }
)

LIVE_PK = (
    "fixture_id",
    "bet_id",
    "value_label",
    "handicap",
    "captured_at",
)

ETL_STATUSES = frozenset(
    {
        "pending",
        "in_progress",
        "complete",
        "coverage_empty",
        "not_supported",
        "retryable_error",
        "permanent_error",
    }
)


def test_migrated_schema_has_domain_and_etl_tables() -> None:
    with app_connect() as conn:
        rows = conn.execute("""
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            """).fetchall()
    names = {row[0] for row in rows}
    missing = EXPECTED_TABLES - names
    assert not missing, f"missing tables: {sorted(missing)}"
    assert "etl_lease" not in names
    assert "alembic_version" in names


def test_alembic_head_is_etl_state() -> None:
    with app_connect() as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version is not None
    assert version[0] == "0017_etl_state"


def test_fixture_odds_live_pk_is_append_only() -> None:
    with app_connect() as conn:
        rows = conn.execute("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid
             AND a.attnum = ANY (i.indkey)
            WHERE i.indrelid = 'fixture_odds_live'::regclass
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """).fetchall()
    columns = tuple(row[0] for row in rows)
    assert columns == LIVE_PK


def test_etl_tasks_rejects_unknown_status() -> None:
    with app_connect() as conn:
        conn.execute("BEGIN")
        with pytest.raises(CheckViolation):
            conn.execute("""
                INSERT INTO etl_tasks (endpoint, params, status)
                VALUES ('/status', '{}'::jsonb, 'lease')
                """)
        conn.execute("ROLLBACK")


def test_etl_tasks_accepts_fr027_statuses() -> None:
    with app_connect() as conn:
        conn.execute("BEGIN")
        for status in sorted(ETL_STATUSES):
            conn.execute(
                """
                INSERT INTO etl_tasks (endpoint, params, status)
                VALUES (%s, '{}'::jsonb, %s)
                """,
                (f"/test/{status}", status),
            )
        conn.execute("ROLLBACK")


def test_fixture_statuses_seeded() -> None:
    with app_connect() as conn:
        count = conn.execute("SELECT count(*) FROM fixture_statuses").fetchone()
    assert count is not None
    assert count[0] == 19


def test_team_season_statistics_pk_includes_as_of_date() -> None:
    with app_connect() as conn:
        rows = conn.execute("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid
             AND a.attnum = ANY (i.indkey)
            WHERE i.indrelid = 'team_season_statistics'::regclass
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """).fetchall()
    columns = tuple(row[0] for row in rows)
    assert columns == ("team_id", "league_id", "season", "as_of_date")
