"""0026 widens advisor_cycles validation_status; downgrade refuses new values."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config

from tests.db import app_connect


def _alembic() -> Config:
    return Config("alembic.ini")


def _seed_strategy(conn) -> tuple[int, int]:
    strategy_id = conn.execute("""
        INSERT INTO advisor_strategies (name, kind, status)
        VALUES ('cycle-single-rules', 'llm', 'sandbox')
        RETURNING id
        """).fetchone()[0]
    version_id = conn.execute(
        """
        INSERT INTO advisor_strategy_versions (
            strategy_id, version_no, prompt_version, min_combined_odd, max_legs,
            max_minute_regulation, max_minute_extra, wait_minutes, candidate_rules,
            notes
        )
        VALUES (
            %s, 1, 'p5', 1.600, 3, 80, 110, 10, '["favorite_losing"]'::jsonb, ''
        )
        RETURNING id
        """,
        (strategy_id,),
    ).fetchone()[0]
    return int(strategy_id), int(version_id)


def _cleanup(conn, strategy_id: int, version_id: int) -> None:
    conn.execute(
        "DELETE FROM advisor_cycles WHERE strategy_version_id = %s", (version_id,)
    )
    conn.execute("DELETE FROM advisor_strategy_versions WHERE id = %s", (version_id,))
    conn.execute("DELETE FROM advisor_strategies WHERE id = %s", (strategy_id,))


def test_cycle_single_rules_migration_roundtrip() -> None:
    command.upgrade(_alembic(), "head")
    strategy_id = 0
    version_id = 0
    try:
        with app_connect() as conn, conn.transaction():
            strategy_id, version_id = _seed_strategy(conn)
            conn.execute(
                """
                    INSERT INTO advisor_cycles (
                        strategy_version_id, mode, provider, model, model_version,
                        prompt_version, prompt_text, input_json, raw_output,
                        validation_status, input_tokens, output_tokens,
                        thoughts_tokens, cost_pln
                    )
                    VALUES (
                        %s, 'sandbox', 'google', 'gemini-2.5-flash',
                        'gemini-2.5-flash', 'p5', 'x', '{}'::jsonb, '{}',
                        'single_negative_ev', 0, 0, 0, 0
                    )
                    """,
                (version_id,),
            )

        with pytest.raises(
            RuntimeError, match="single_negative_ev|single_with_partners"
        ):
            command.downgrade(_alembic(), "0025_advisor_odds_refresh")

        with app_connect() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM advisor_cycles WHERE strategy_version_id = %s",
                (version_id,),
            )

        command.downgrade(_alembic(), "0025_advisor_odds_refresh")
        command.upgrade(_alembic(), "head")

        with app_connect() as conn, conn.transaction():
            conn.execute(
                """
                    INSERT INTO advisor_cycles (
                        strategy_version_id, mode, provider, model, model_version,
                        prompt_version, prompt_text, input_json, raw_output,
                        validation_status, input_tokens, output_tokens,
                        thoughts_tokens, cost_pln
                    )
                    VALUES (
                        %s, 'sandbox', 'google', 'gemini-2.5-flash',
                        'gemini-2.5-flash', 'p5', 'x', '{}'::jsonb, '{}',
                        'single_with_partners', 0, 0, 0, 0
                    )
                    """,
                (version_id,),
            )
    finally:
        command.upgrade(_alembic(), "head")
        with app_connect() as conn, conn.transaction():
            _cleanup(conn, strategy_id, version_id)
