"""Checkpoint for the recommendation graph and a sandbox-llm play claim.

The cycle row stays insert-only. These two tables may change status.
"""

from alembic import op

revision: str = "0027_advisor_graph_play"
down_revision: str | None = "0026_cycle_single_rules"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
CREATE TABLE advisor_graph_checkpoint (
    cycle_id    BIGINT PRIMARY KEY
        REFERENCES advisor_cycles(id) ON DELETE RESTRICT,
    node_name   VARCHAR(32) NOT NULL,
    state       JSONB NOT NULL,
    status      VARCHAR(16) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_graph_checkpoint_status_check
        CHECK (status IN ('running', 'waiting_human', 'done'))
);

CREATE TABLE advisor_play_claims (
    id                       BIGSERIAL PRIMARY KEY,
    sandbox_coupon_id        BIGINT NOT NULL UNIQUE
        REFERENCES advisor_coupons(id) ON DELETE RESTRICT,
    status                   VARCHAR(16) NOT NULL,
    combined_odd             NUMERIC(10, 3),
    stake                    NUMERIC(12, 2),
    recommendation_coupon_id BIGINT
        REFERENCES advisor_coupons(id) ON DELETE RESTRICT,
    asked_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at              TIMESTAMPTZ,
    CONSTRAINT advisor_play_claims_status_check
        CHECK (status IN ('pending', 'played', 'not_played')),
    CONSTRAINT advisor_play_claims_distinct_coupon_check
        CHECK (
            recommendation_coupon_id IS NULL
            OR recommendation_coupon_id <> sandbox_coupon_id
        ),
    CONSTRAINT advisor_play_claims_shape_check CHECK (
        (
            status = 'pending'
            AND combined_odd IS NULL
            AND stake IS NULL
            AND recommendation_coupon_id IS NULL
            AND answered_at IS NULL
        )
        OR (
            status = 'played'
            AND combined_odd > 1
            AND stake > 0
            AND recommendation_coupon_id IS NOT NULL
            AND answered_at IS NOT NULL
        )
        OR (
            status = 'not_played'
            AND combined_odd IS NULL
            AND stake IS NULL
            AND recommendation_coupon_id IS NULL
            AND answered_at IS NOT NULL
        )
    )
)
""")


def downgrade() -> None:
    _exec_sql("""
DROP TABLE IF EXISTS advisor_play_claims;
DROP TABLE IF EXISTS advisor_graph_checkpoint
""")
