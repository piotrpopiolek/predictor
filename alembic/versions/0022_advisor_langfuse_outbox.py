"""Queue of Langfuse exports. A failed send is retried; the cycle is not updated."""

from alembic import op

revision: str = "0022_advisor_langfuse_outbox"
down_revision: str | None = "0021_advisor_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
CREATE TABLE advisor_langfuse_outbox (
    id          BIGSERIAL PRIMARY KEY,
    cycle_id    BIGINT NOT NULL UNIQUE
        REFERENCES advisor_cycles(id) ON DELETE RESTRICT,
    trace_id    VARCHAR(64) NOT NULL,
    payload     JSONB NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    last_error  VARCHAR(128),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_langfuse_outbox_status_check
        CHECK (status IN ('pending', 'sending', 'sent', 'abandoned')),
    CONSTRAINT advisor_langfuse_outbox_attempts_check
        CHECK (attempts >= 0 AND attempts <= 5)
);

CREATE INDEX idx_advisor_langfuse_outbox_pending
    ON advisor_langfuse_outbox (id)
    WHERE status = 'pending'
""")


def downgrade() -> None:
    _exec_sql("DROP TABLE IF EXISTS advisor_langfuse_outbox")
