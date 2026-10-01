"""Leagues the worker does not follow live and does not build history for."""

from alembic import op

revision: str = "0020_untracked_leagues"
down_revision: str | None = "0019_operator_bets_no_side"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
CREATE TABLE untracked_leagues (
    league_id   INT PRIMARY KEY REFERENCES leagues(id),
    note        VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO leagues (id) VALUES
    (10),
    (666),
    (667),
    (60),
    (59),
    (931),
    (58),
    (487),
    (148),
    (149),
    (150),
    (1067)
ON CONFLICT (id) DO NOTHING;
INSERT INTO untracked_leagues (league_id, note) VALUES
    (10, 'friendlies'),
    (666, 'friendlies'),
    (667, 'friendlies'),
    (60, 'non-league'),
    (59, 'non-league'),
    (931, 'non-league'),
    (58, 'non-league'),
    (487, 'amateur'),
    (148, 'amateur'),
    (149, 'amateur'),
    (150, 'amateur'),
    (1067, 'amateur')
ON CONFLICT (league_id) DO NOTHING
""")


def downgrade() -> None:
    _exec_sql("DROP TABLE IF EXISTS untracked_leagues")
