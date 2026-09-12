"""T017 ETL state: etl_runs, etl_tasks. No etl_lease."""

from alembic import op

revision: str = "0017_etl_state"
down_revision: str | None = "0016_catalog_rest"
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
CREATE TABLE etl_runs (
    id                   BIGSERIAL PRIMARY KEY,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at             TIMESTAMPTZ,
    host                 VARCHAR(255) NOT NULL,
    host_environment     VARCHAR(8) NOT NULL,
    pid                  INT NOT NULL,
    instance_id          VARCHAR(128) NOT NULL,
    lock_held            BOOLEAN NOT NULL DEFAULT false,
    postgres_backend_pid INT,
    CONSTRAINT etl_runs_host_environment_check
        CHECK (host_environment IN ('local', 'vps'))
);

CREATE INDEX idx_etl_runs_started ON etl_runs(started_at DESC);

CREATE TABLE etl_tasks (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT REFERENCES etl_runs(id) ON DELETE SET NULL,
    endpoint        VARCHAR(128) NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    cursor_kind     VARCHAR(16),
    fixture_id      BIGINT REFERENCES fixtures(id) ON DELETE SET NULL,
    day_utc         DATE,
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count   INT NOT NULL DEFAULT 0,
    last_error      TEXT,
    paging_current  INT,
    paging_total    INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMPTZ,
    CONSTRAINT etl_tasks_status_check CHECK (status IN (
        'pending',
        'in_progress',
        'complete',
        'coverage_empty',
        'not_supported',
        'retryable_error',
        'permanent_error'
    )),
    CONSTRAINT etl_tasks_cursor_kind_check CHECK (
        cursor_kind IS NULL
        OR cursor_kind IN ('forward', 'backfill', 'enrichment')
    )
);

CREATE INDEX idx_etl_tasks_status ON etl_tasks(status);
CREATE INDEX idx_etl_tasks_endpoint ON etl_tasks(endpoint);
CREATE INDEX idx_etl_tasks_day_utc ON etl_tasks(day_utc);
CREATE INDEX idx_etl_tasks_fixture ON etl_tasks(fixture_id);
CREATE INDEX idx_etl_tasks_in_progress ON etl_tasks(id) WHERE status = 'in_progress'
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS etl_tasks")
    op.execute("DROP TABLE IF EXISTS etl_runs")
