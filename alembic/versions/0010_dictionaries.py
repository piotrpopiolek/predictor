"""T010 dictionaries: countries, seasons, venues, leagues, rounds, fixture_statuses."""

from alembic import op

revision: str = "0010_dictionaries"
down_revision: str | None = None
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
CREATE TABLE countries (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL UNIQUE,
    code          VARCHAR(8),
    flag          TEXT
);

CREATE TABLE seasons (
    year INT PRIMARY KEY
);

CREATE TABLE venues (
    id            INT PRIMARY KEY,
    name          VARCHAR(255),
    address       TEXT,
    city          VARCHAR(100),
    country_name  VARCHAR(255) REFERENCES countries(name),
    capacity      INT,
    surface       VARCHAR(50),
    image         TEXT
);

CREATE TABLE leagues (
    id            INT PRIMARY KEY,
    name          VARCHAR(255),
    type          VARCHAR(20),
    logo          TEXT,
    country_name  VARCHAR(255) REFERENCES countries(name),
    country_code  VARCHAR(8)
);

CREATE TABLE league_seasons (
    league_id                  INT NOT NULL REFERENCES leagues(id),
    season_year                INT NOT NULL,
    start_date                 DATE,
    end_date                   DATE,
    is_current                 BOOLEAN,
    cov_events                 BOOLEAN,
    cov_lineups                BOOLEAN,
    cov_statistics_fixtures    BOOLEAN,
    cov_statistics_players     BOOLEAN,
    cov_standings              BOOLEAN,
    cov_players                BOOLEAN,
    cov_top_scorers            BOOLEAN,
    cov_top_assists            BOOLEAN,
    cov_top_cards              BOOLEAN,
    cov_injuries               BOOLEAN,
    cov_predictions            BOOLEAN,
    cov_odds                   BOOLEAN,
    PRIMARY KEY (league_id, season_year)
);

CREATE TABLE league_rounds (
    league_id   INT NOT NULL REFERENCES leagues(id),
    season      INT NOT NULL,
    round_name  VARCHAR(100) NOT NULL,
    dates       DATE[],
    PRIMARY KEY (league_id, season, round_name)
);

CREATE TABLE fixture_statuses (
    code  VARCHAR(10) PRIMARY KEY,
    label VARCHAR(100) NOT NULL
);

INSERT INTO fixture_statuses (code, label) VALUES
('TBD',  'Time To Be Defined'),
('NS',   'Not Started'),
('1H',   'First Half'),
('HT',   'Halftime'),
('2H',   'Second Half'),
('ET',   'Extra Time'),
('BT',   'Break Time'),
('P',    'Penalty In Progress'),
('SUSP', 'Match Suspended'),
('INT',  'Match Interrupted'),
('LIVE', 'In Progress'),
('FT',   'Match Finished'),
('AET',  'Match Finished After Extra Time'),
('PEN',  'Match Finished After Penalty'),
('PST',  'Match Postponed'),
('CANC', 'Match Cancelled'),
('ABD',  'Match Abandoned'),
('AWD',  'Technical Loss'),
('WO',   'WalkOver')
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS league_rounds")
    op.execute("DROP TABLE IF EXISTS league_seasons")
    op.execute("DROP TABLE IF EXISTS leagues")
    op.execute("DROP TABLE IF EXISTS venues")
    op.execute("DROP TABLE IF EXISTS fixture_statuses")
    op.execute("DROP TABLE IF EXISTS seasons")
    op.execute("DROP TABLE IF EXISTS countries")
