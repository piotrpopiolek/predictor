"""Operator coupon actions and Telegram retries. Cycles stay insert-only."""

from alembic import op

revision: str = "0023_advisor_coupon_events"
down_revision: str | None = "0022_advisor_langfuse_outbox"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
ALTER TABLE advisor_coupons
    ADD COLUMN proposed_stake NUMERIC(12, 2);

CREATE TABLE advisor_coupon_events (
    id               BIGSERIAL PRIMARY KEY,
    coupon_id        BIGINT NOT NULL
        REFERENCES advisor_coupons(id) ON DELETE RESTRICT,
    previous_status  VARCHAR(16) NOT NULL,
    new_status       VARCHAR(16) NOT NULL,
    source           VARCHAR(16) NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_coupon_events_source_check
        CHECK (source IN ('operator', 'expire', 'withdraw', 'propose'))
);

CREATE TABLE advisor_strategy_events (
    id               BIGSERIAL PRIMARY KEY,
    strategy_id      BIGINT NOT NULL
        REFERENCES advisor_strategies(id) ON DELETE RESTRICT,
    previous_status  VARCHAR(16) NOT NULL,
    new_status       VARCHAR(16) NOT NULL,
    source           VARCHAR(16) NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_strategy_events_source_check
        CHECK (source IN ('operator', 'withdraw', 'summary'))
);

CREATE TABLE advisor_telegram_outbox (
    id          BIGSERIAL PRIMARY KEY,
    kind        VARCHAR(32) NOT NULL,
    payload     JSONB NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    last_error  VARCHAR(32),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_telegram_outbox_status_check
        CHECK (status IN ('pending', 'sending', 'sent', 'abandoned')),
    CONSTRAINT advisor_telegram_outbox_attempts_check
        CHECK (attempts >= 0 AND attempts <= 5)
);

CREATE INDEX idx_advisor_telegram_outbox_pending
    ON advisor_telegram_outbox (id)
    WHERE status = 'pending'
""")


def downgrade() -> None:
    _exec_sql("""
DROP TABLE IF EXISTS advisor_telegram_outbox;
DROP TABLE IF EXISTS advisor_strategy_events;
DROP TABLE IF EXISTS advisor_coupon_events;
ALTER TABLE advisor_coupons DROP COLUMN IF EXISTS proposed_stake
""")
