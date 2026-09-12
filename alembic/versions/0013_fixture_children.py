"""T013 fixture children: events, lineups, statistics, player stats."""

from alembic import op

revision: str = "0013_fixture_children"
down_revision: str | None = "0012_fixtures"
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
CREATE TABLE fixture_events (
    id                BIGSERIAL PRIMARY KEY,
    fixture_id        BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    team_id           INT NOT NULL REFERENCES teams(id),
    player_id         INT REFERENCES players(id),
    assist_player_id  INT REFERENCES players(id),
    event_type        VARCHAR(16) NOT NULL,
    detail            VARCHAR(100),
    minute            INT NOT NULL,
    minute_extra      INT,
    comments          TEXT,
    sort_order        INT
);

CREATE INDEX idx_fixture_events_fixture ON fixture_events(fixture_id);
CREATE INDEX idx_fixture_events_player ON fixture_events(player_id);

CREATE TABLE fixture_lineups (
    fixture_id  BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    team_id     INT NOT NULL REFERENCES teams(id),
    coach_id    INT REFERENCES coaches(id),
    formation   VARCHAR(20),
    is_home     BOOLEAN NOT NULL,
    colors      JSONB,
    PRIMARY KEY (fixture_id, team_id)
);

CREATE TABLE fixture_lineup_players (
    fixture_id  BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    team_id     INT NOT NULL REFERENCES teams(id),
    player_id   INT NOT NULL REFERENCES players(id),
    number      INT,
    position    VARCHAR(8),
    grid        VARCHAR(10),
    is_starter  BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (fixture_id, team_id, player_id),
    FOREIGN KEY (fixture_id, team_id)
        REFERENCES fixture_lineups(fixture_id, team_id) ON DELETE CASCADE
);

CREATE TABLE fixture_statistics (
    fixture_id  BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    team_id     INT NOT NULL REFERENCES teams(id),
    stat_type   VARCHAR(100) NOT NULL,
    stat_value  VARCHAR(50),
    period      VARCHAR(4) NOT NULL DEFAULT 'FT',
    PRIMARY KEY (fixture_id, team_id, stat_type, period)
);

CREATE TABLE fixture_player_stats (
    fixture_id          BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    team_id             INT NOT NULL REFERENCES teams(id),
    player_id           INT NOT NULL REFERENCES players(id),
    stats_updated_at    TIMESTAMPTZ,
    minutes             INT,
    number              INT,
    position            VARCHAR(32),
    rating              VARCHAR(8),
    captain             BOOLEAN,
    substitute          BOOLEAN,
    offsides            INT,
    shots_total         INT,
    shots_on            INT,
    goals               INT,
    goals_conceded      INT,
    assists             INT,
    saves               INT,
    passes_total        INT,
    passes_key          INT,
    passes_accuracy     VARCHAR(16),
    tackles             INT,
    blocks              INT,
    interceptions       INT,
    duels_total         INT,
    duels_won           INT,
    dribbles_attempts   INT,
    dribbles_success    INT,
    dribbles_past       INT,
    fouls_drawn         INT,
    fouls_committed     INT,
    yellow_cards        INT,
    red_cards           INT,
    pen_won             INT,
    pen_committed       INT,
    pen_scored          INT,
    pen_missed          INT,
    pen_saved           INT,
    PRIMARY KEY (fixture_id, team_id, player_id)
);

CREATE INDEX idx_fixture_player_stats_player ON fixture_player_stats(player_id)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fixture_player_stats")
    op.execute("DROP TABLE IF EXISTS fixture_statistics")
    op.execute("DROP TABLE IF EXISTS fixture_lineup_players")
    op.execute("DROP TABLE IF EXISTS fixture_lineups")
    op.execute("DROP TABLE IF EXISTS fixture_events")
