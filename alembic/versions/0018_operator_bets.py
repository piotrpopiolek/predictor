"""Operator next-goal bet ledger. Outside phase-1 ETL mirror."""

from alembic import op

revision: str = "0018_operator_bets"
down_revision: str | None = "0017_etl_state"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql(
        """
CREATE TABLE operator_bets (
    id              BIGSERIAL PRIMARY KEY,
    fixture_id      BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE RESTRICT,
    selection       VARCHAR(8) NOT NULL,
    odd             NUMERIC(10,3) NOT NULL,
    stake           NUMERIC(12,2) NOT NULL,
    status          VARCHAR(8) NOT NULL DEFAULT 'open',
    placed_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at      TIMESTAMPTZ,
    league          VARCHAR(255) NOT NULL DEFAULT '',
    home_name       VARCHAR(255) NOT NULL DEFAULT '',
    away_name       VARCHAR(255) NOT NULL DEFAULT '',
    goals_home      INT NOT NULL DEFAULT 0,
    goals_away      INT NOT NULL DEFAULT 0,
    elapsed         INT,
    status_short    VARCHAR(10),
    payout_keep     NUMERIC(4,3) NOT NULL DEFAULT 0.880,
    CONSTRAINT operator_bets_selection_check
        CHECK (selection IN ('home', 'none', 'away')),
    CONSTRAINT operator_bets_status_check
        CHECK (status IN ('open', 'won', 'lost', 'void')),
    CONSTRAINT operator_bets_odd_check
        CHECK (odd >= 1.200 AND odd <= 6.000),
    CONSTRAINT operator_bets_stake_check
        CHECK (stake > 0)
);

CREATE UNIQUE INDEX uq_operator_bets_open_fixture
    ON operator_bets (fixture_id) WHERE status = 'open';

CREATE INDEX idx_operator_bets_placed ON operator_bets (placed_at DESC);
CREATE INDEX idx_operator_bets_status ON operator_bets (status)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS operator_bets")
