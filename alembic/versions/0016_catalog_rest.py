"""T016 transfers, injuries, sidelined, coach career, trophies."""

from alembic import op

revision: str = "0016_catalog_rest"
down_revision: str | None = "0015_odds_predictions"
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
CREATE TABLE transfers (
    id             BIGSERIAL PRIMARY KEY,
    player_id      INT NOT NULL REFERENCES players(id),
    from_team_id   INT REFERENCES teams(id),
    to_team_id     INT REFERENCES teams(id),
    date           DATE,
    type           VARCHAR(50),
    api_update     TIMESTAMPTZ
);

CREATE INDEX idx_transfers_player ON transfers(player_id);
CREATE UNIQUE INDEX uniq_transfers_natural ON transfers (
    player_id,
    COALESCE(date, '1970-01-01'::date),
    COALESCE(from_team_id, 0),
    COALESCE(to_team_id, 0),
    COALESCE(type, '')
);

CREATE TABLE injuries (
    id              BIGSERIAL PRIMARY KEY,
    player_id       INT NOT NULL REFERENCES players(id),
    team_id         INT NOT NULL REFERENCES teams(id),
    fixture_id      BIGINT REFERENCES fixtures(id),
    league_id       INT REFERENCES leagues(id),
    season          INT,
    availability    VARCHAR(32),
    reason          TEXT,
    UNIQUE (fixture_id, player_id)
);

CREATE INDEX idx_injuries_player ON injuries(player_id);
CREATE INDEX idx_injuries_team ON injuries(team_id);

CREATE TABLE sidelined (
    id          BIGSERIAL PRIMARY KEY,
    player_id   INT REFERENCES players(id),
    coach_id    INT REFERENCES coaches(id),
    type        VARCHAR(100) NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE,
    CONSTRAINT sidelined_owner_check CHECK (
        (player_id IS NOT NULL AND coach_id IS NULL)
        OR (player_id IS NULL AND coach_id IS NOT NULL)
    )
);

CREATE INDEX idx_sidelined_player ON sidelined(player_id) WHERE player_id IS NOT NULL;
CREATE INDEX idx_sidelined_coach ON sidelined(coach_id) WHERE coach_id IS NOT NULL;
CREATE UNIQUE INDEX uniq_sidelined_player
    ON sidelined (player_id, type, start_date) WHERE player_id IS NOT NULL;
CREATE UNIQUE INDEX uniq_sidelined_coach
    ON sidelined (coach_id, type, start_date) WHERE coach_id IS NOT NULL;

CREATE TABLE coach_career (
    coach_id    INT NOT NULL REFERENCES coaches(id) ON DELETE CASCADE,
    team_id     INT NOT NULL REFERENCES teams(id),
    start_date  DATE NOT NULL,
    end_date    DATE,
    PRIMARY KEY (coach_id, team_id, start_date)
);

CREATE TABLE trophies (
    id           BIGSERIAL PRIMARY KEY,
    player_id    INT REFERENCES players(id),
    coach_id     INT REFERENCES coaches(id),
    league_name  VARCHAR(255),
    country      VARCHAR(100),
    season       VARCHAR(32),
    place        VARCHAR(100),
    CONSTRAINT trophies_owner_check CHECK (
        (player_id IS NOT NULL AND coach_id IS NULL)
        OR (player_id IS NULL AND coach_id IS NOT NULL)
    )
);

CREATE INDEX idx_trophies_player ON trophies(player_id) WHERE player_id IS NOT NULL;
CREATE INDEX idx_trophies_coach ON trophies(coach_id) WHERE coach_id IS NOT NULL
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trophies")
    op.execute("DROP TABLE IF EXISTS coach_career")
    op.execute("DROP TABLE IF EXISTS sidelined")
    op.execute("DROP TABLE IF EXISTS injuries")
    op.execute("DROP TABLE IF EXISTS transfers")
