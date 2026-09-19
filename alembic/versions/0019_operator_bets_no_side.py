"""Drop operator_bets.selection — always next goal will be scored."""

from alembic import op

revision: str = "0019_operator_bets_no_side"
down_revision: str | None = "0018_operator_bets"
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
ALTER TABLE operator_bets DROP CONSTRAINT IF EXISTS operator_bets_selection_check;
ALTER TABLE operator_bets DROP COLUMN IF EXISTS selection
"""
    )


def downgrade() -> None:
    _exec_sql(
        """
ALTER TABLE operator_bets
    ADD COLUMN IF NOT EXISTS selection VARCHAR(8) NOT NULL DEFAULT 'none';
ALTER TABLE operator_bets DROP CONSTRAINT IF EXISTS operator_bets_selection_check;
ALTER TABLE operator_bets
    ADD CONSTRAINT operator_bets_selection_check
    CHECK (selection IN ('home', 'none', 'away'))
"""
    )
