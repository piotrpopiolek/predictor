"""T011 parent entities: teams, players, coaches, bookmakers, odds dictionaries."""

from alembic import op

revision: str = "0011_parent_entities"
down_revision: str | None = "0010_dictionaries"
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
CREATE TABLE teams (
    id            INT PRIMARY KEY,
    name          VARCHAR(255),
    code          VARCHAR(10),
    country_name  VARCHAR(255) REFERENCES countries(name),
    founded       INT,
    national      BOOLEAN DEFAULT false,
    logo          TEXT,
    venue_id      INT REFERENCES venues(id)
);

CREATE TABLE players (
    id            INT PRIMARY KEY,
    name          VARCHAR(255),
    firstname     VARCHAR(100),
    lastname      VARCHAR(100),
    age           INT,
    birth_date    DATE,
    birth_place   VARCHAR(255),
    birth_country VARCHAR(100),
    nationality   VARCHAR(100),
    height        VARCHAR(20),
    weight        VARCHAR(20),
    injured       BOOLEAN,
    photo         TEXT
);

CREATE TABLE coaches (
    id            INT PRIMARY KEY,
    name          VARCHAR(255),
    firstname     VARCHAR(100),
    lastname      VARCHAR(100),
    age           INT,
    birth_date    DATE,
    birth_place   VARCHAR(255),
    birth_country VARCHAR(100),
    nationality   VARCHAR(100),
    height        VARCHAR(20),
    weight        VARCHAR(20),
    photo         TEXT,
    team_id       INT REFERENCES teams(id)
);

CREATE TABLE bookmakers (
    id   INT PRIMARY KEY,
    name VARCHAR(255)
);

CREATE TABLE odds_bets (
    id   INT PRIMARY KEY,
    name VARCHAR(255)
);

CREATE TABLE odds_live_bets (
    id   INT PRIMARY KEY,
    name VARCHAR(255)
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS odds_live_bets")
    op.execute("DROP TABLE IF EXISTS odds_bets")
    op.execute("DROP TABLE IF EXISTS bookmakers")
    op.execute("DROP TABLE IF EXISTS coaches")
    op.execute("DROP TABLE IF EXISTS players")
    op.execute("DROP TABLE IF EXISTS teams")
