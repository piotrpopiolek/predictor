"""Advisor asks the worker for a targeted live odds refresh."""

from alembic import op

revision: str = "0025_advisor_odds_refresh"
down_revision: str | None = "0024_cycle_validation_status"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
CREATE TABLE advisor_odds_refresh (
    fixture_id    BIGINT PRIMARY KEY
        REFERENCES fixtures(id) ON DELETE CASCADE,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")


def downgrade() -> None:
    _exec_sql("DROP TABLE IF EXISTS advisor_odds_refresh")
