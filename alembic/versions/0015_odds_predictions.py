"""T015 predictions and odds; fixture_odds_live is append-only (captured_at in PK)."""

from alembic import op

revision: str = "0015_odds_predictions"
down_revision: str | None = "0014_seasonal"
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
CREATE TABLE predictions (
    fixture_id       BIGINT PRIMARY KEY REFERENCES fixtures(id) ON DELETE CASCADE,
    winner_team_id   INT REFERENCES teams(id),
    winner_comment   TEXT,
    win_or_draw      BOOLEAN,
    under_over       VARCHAR(16),
    goals_home       VARCHAR(16),
    goals_away       VARCHAR(16),
    advice           TEXT,
    pct_home         VARCHAR(8),
    pct_draw         VARCHAR(8),
    pct_away         VARCHAR(8),
    comparison       JSONB,
    teams            JSONB,
    synced_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE prediction_h2h (
    fixture_id      BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    h2h_fixture_id  BIGINT NOT NULL REFERENCES fixtures(id),
    sort_order      INT,
    PRIMARY KEY (fixture_id, h2h_fixture_id)
);

CREATE TABLE fixture_odds (
    fixture_id    BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    bookmaker_id  INT NOT NULL REFERENCES bookmakers(id),
    bet_id        INT NOT NULL REFERENCES odds_bets(id),
    value_label   VARCHAR(64) NOT NULL,
    odd           NUMERIC(10,3) NOT NULL,
    total         NUMERIC(6,2),
    handicap      NUMERIC(6,2),
    api_update    TIMESTAMPTZ,
    PRIMARY KEY (fixture_id, bookmaker_id, bet_id, value_label)
);

CREATE INDEX idx_fixture_odds_fixture ON fixture_odds(fixture_id);

CREATE TABLE fixture_odds_live (
    fixture_id      BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    bet_id          INT NOT NULL REFERENCES odds_live_bets(id),
    value_label     VARCHAR(64) NOT NULL,
    handicap        VARCHAR(16) NOT NULL DEFAULT '',
    odd             NUMERIC(10,3) NOT NULL,
    is_main         BOOLEAN,
    suspended       BOOLEAN,
    stopped         BOOLEAN,
    blocked         BOOLEAN,
    finished        BOOLEAN,
    status_long     VARCHAR(100),
    elapsed_minutes INT,
    elapsed_seconds VARCHAR(16),
    league_id       INT,
    season          INT,
    home_goals      INT,
    away_goals      INT,
    api_update      TIMESTAMPTZ,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fixture_id, bet_id, value_label, handicap, captured_at)
);

CREATE INDEX idx_fixture_odds_live_fixture ON fixture_odds_live(fixture_id);
CREATE INDEX idx_fixture_odds_live_captured
    ON fixture_odds_live(fixture_id, captured_at DESC);

CREATE TABLE odds_fixture_mapping (
    fixture_id  BIGINT PRIMARY KEY REFERENCES fixtures(id) ON DELETE CASCADE,
    league_id   INT REFERENCES leagues(id),
    season      INT,
    synced_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS odds_fixture_mapping")
    op.execute("DROP TABLE IF EXISTS fixture_odds_live")
    op.execute("DROP TABLE IF EXISTS fixture_odds")
    op.execute("DROP TABLE IF EXISTS prediction_h2h")
    op.execute("DROP TABLE IF EXISTS predictions")
