"""Semantic cycle failures keep their own status, not invalid_output."""

from alembic import op

revision: str = "0024_cycle_validation_status"
down_revision: str | None = "0023_advisor_coupon_events"
branch_labels: str | None = None
depends_on: str | None = None

_STATUSES = (
    "'ok', 'invalid_output', 'provider_error', "
    "'duplicate_candidate', 'candidate_set', 'duplicate_leg', "
    "'too_many_legs', 'unknown_fixture', 'open_coupon', "
    "'invalid_odd', 'below_threshold', 'bet_not_in_coupon', 'not_a_bet'"
)


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql(f"""
ALTER TABLE advisor_cycles
    DROP CONSTRAINT advisor_cycles_validation_check;

ALTER TABLE advisor_cycles
    ALTER COLUMN validation_status TYPE VARCHAR(32);

ALTER TABLE advisor_cycles
    ADD CONSTRAINT advisor_cycles_validation_check
    CHECK (validation_status IN ({_STATUSES}))
""")


def downgrade() -> None:
    _exec_sql("""
ALTER TABLE advisor_cycles
    DROP CONSTRAINT advisor_cycles_validation_check;

ALTER TABLE advisor_cycles
    ALTER COLUMN validation_status TYPE VARCHAR(16);

ALTER TABLE advisor_cycles
    ADD CONSTRAINT advisor_cycles_validation_check
    CHECK (validation_status IN ('ok', 'invalid_output', 'provider_error'))
""")
