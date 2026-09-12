"""T012 fixtures pień grafu."""

from alembic import op

revision: str = "0012_fixtures"
down_revision: str | None = "0011_parent_entities"
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
CREATE TABLE fixtures (
    id                     BIGINT PRIMARY KEY,
    referee                VARCHAR(255),
    timezone               VARCHAR(64),
    date                   TIMESTAMPTZ,
    timestamp_utc          BIGINT,
    period_first           BIGINT,
    period_second          BIGINT,
    venue_id               INT REFERENCES venues(id),
    venue_name             VARCHAR(255),
    venue_city             VARCHAR(100),
    status_short           VARCHAR(10) REFERENCES fixture_statuses(code),
    status_long            VARCHAR(100),
    elapsed_minutes        INT,
    extra_minutes          INT,
    league_id              INT NOT NULL REFERENCES leagues(id),
    season                 INT NOT NULL,
    round                  VARCHAR(100),
    home_team_id           INT NOT NULL REFERENCES teams(id),
    away_team_id           INT NOT NULL REFERENCES teams(id),
    home_winner            BOOLEAN,
    away_winner            BOOLEAN,
    goals_home             INT,
    goals_away             INT,
    goals_home_halftime    INT,
    goals_away_halftime    INT,
    goals_home_fulltime    INT,
    goals_away_fulltime    INT,
    goals_home_extratime   INT,
    goals_away_extratime   INT,
    goals_home_penalty     INT,
    goals_away_penalty     INT,
    tie_id                 BIGINT,
    leg                    SMALLINT,
    synced_at              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_fixtures_league_season ON fixtures(league_id, season);
CREATE INDEX idx_fixtures_date ON fixtures(date);
CREATE INDEX idx_fixtures_home_away ON fixtures(home_team_id, away_team_id);
CREATE INDEX idx_fixtures_venue ON fixtures(venue_id);
CREATE INDEX idx_fixtures_status ON fixtures(status_short);
CREATE INDEX idx_fixtures_elapsed ON fixtures(elapsed_minutes) WHERE elapsed_minutes IS NOT NULL
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fixtures")
