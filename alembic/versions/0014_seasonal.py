"""T014 seasonal aggregates: standings, player stats, squads, career, team season."""

from alembic import op

revision: str = "0014_seasonal"
down_revision: str | None = "0013_fixture_children"
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
CREATE TABLE standings (
    id              BIGSERIAL PRIMARY KEY,
    league_id       INT NOT NULL REFERENCES leagues(id),
    season          INT NOT NULL,
    group_name      VARCHAR(100) NOT NULL DEFAULT '',
    rank            INT NOT NULL,
    team_id         INT NOT NULL REFERENCES teams(id),
    points          INT,
    goals_diff      INT,
    form            VARCHAR(50),
    status          VARCHAR(16),
    description     TEXT,
    api_update      TIMESTAMPTZ,
    played          INT,
    win             INT,
    draw            INT,
    lose            INT,
    goals_for       INT,
    goals_against   INT,
    home_played     INT,
    home_win        INT,
    home_draw       INT,
    home_lose       INT,
    home_gf         INT,
    home_ga         INT,
    away_played     INT,
    away_win        INT,
    away_draw       INT,
    away_lose       INT,
    away_gf         INT,
    away_ga         INT,
    UNIQUE (league_id, season, group_name, team_id)
);

CREATE INDEX idx_standings_league_season ON standings(league_id, season);
CREATE INDEX idx_standings_team ON standings(team_id);

CREATE TABLE player_statistics (
    id                  BIGSERIAL PRIMARY KEY,
    player_id           INT NOT NULL REFERENCES players(id),
    team_id             INT NOT NULL REFERENCES teams(id),
    league_id           INT NOT NULL REFERENCES leagues(id),
    season              INT NOT NULL,
    appearences         INT,
    lineups             INT,
    minutes             INT,
    number              INT,
    position            VARCHAR(32),
    rating              VARCHAR(8),
    captain             BOOLEAN,
    sub_in              INT,
    sub_out             INT,
    sub_bench           INT,
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
    yellow              INT,
    yellowred           INT,
    red                 INT,
    pen_won             INT,
    pen_committed       INT,
    pen_scored          INT,
    pen_missed          INT,
    pen_saved           INT,
    UNIQUE (player_id, team_id, league_id, season)
);

CREATE INDEX idx_player_statistics_team_league_season
    ON player_statistics(team_id, league_id, season);

CREATE TABLE squad_members (
    team_id    INT NOT NULL REFERENCES teams(id),
    player_id  INT NOT NULL REFERENCES players(id),
    number     INT,
    position   VARCHAR(32),
    PRIMARY KEY (team_id, player_id)
);

CREATE TABLE player_career_teams (
    player_id  INT NOT NULL REFERENCES players(id),
    team_id    INT NOT NULL REFERENCES teams(id),
    season     INT NOT NULL,
    PRIMARY KEY (player_id, team_id, season)
);

CREATE TABLE team_season_statistics (
    team_id            INT NOT NULL REFERENCES teams(id),
    league_id          INT NOT NULL REFERENCES leagues(id),
    season             INT NOT NULL,
    as_of_date         DATE NOT NULL DEFAULT '1970-01-01',
    form               TEXT,
    played_home        INT,
    played_away        INT,
    played_total       INT,
    wins_home          INT,
    wins_away          INT,
    wins_total         INT,
    draws_home         INT,
    draws_away         INT,
    draws_total        INT,
    loses_home         INT,
    loses_away         INT,
    loses_total        INT,
    gf_home            INT,
    gf_away            INT,
    gf_total           INT,
    ga_home            INT,
    ga_away            INT,
    ga_total           INT,
    gf_avg_home        VARCHAR(16),
    gf_avg_away        VARCHAR(16),
    gf_avg_total       VARCHAR(16),
    ga_avg_home        VARCHAR(16),
    ga_avg_away        VARCHAR(16),
    ga_avg_total       VARCHAR(16),
    gf_minute          JSONB,
    ga_minute          JSONB,
    streak_wins        INT,
    streak_draws       INT,
    streak_loses       INT,
    biggest_win_home   VARCHAR(16),
    biggest_win_away   VARCHAR(16),
    biggest_loss_home  VARCHAR(16),
    biggest_loss_away  VARCHAR(16),
    biggest_gf_home    INT,
    biggest_gf_away    INT,
    biggest_ga_home    INT,
    biggest_ga_away    INT,
    cs_home            INT,
    cs_away            INT,
    cs_total           INT,
    fts_home           INT,
    fts_away           INT,
    fts_total          INT,
    pen_scored         INT,
    pen_scored_pct     VARCHAR(16),
    pen_missed         INT,
    pen_missed_pct     VARCHAR(16),
    pen_total          INT,
    lineups            JSONB,
    cards_yellow       JSONB,
    cards_red          JSONB,
    PRIMARY KEY (team_id, league_id, season, as_of_date)
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS team_season_statistics")
    op.execute("DROP TABLE IF EXISTS player_career_teams")
    op.execute("DROP TABLE IF EXISTS squad_members")
    op.execute("DROP TABLE IF EXISTS player_statistics")
    op.execute("DROP TABLE IF EXISTS standings")
