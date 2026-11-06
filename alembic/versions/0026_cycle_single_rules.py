"""Allow single_with_partners and single_negative_ev cycle statuses."""

from alembic import op
from sqlalchemy import text

revision: str = "0026_cycle_single_rules"
down_revision: str | None = "0025_advisor_odds_refresh"
branch_labels: str | None = None
depends_on: str | None = None

_PREVIOUS = (
    "'ok', 'invalid_output', 'provider_error', "
    "'duplicate_candidate', 'candidate_set', 'duplicate_leg', "
    "'too_many_legs', 'unknown_fixture', 'open_coupon', "
    "'invalid_odd', 'below_threshold', 'bet_not_in_coupon', 'not_a_bet'"
)
_STATUSES = f"{_PREVIOUS}, 'single_with_partners', 'single_negative_ev'"
_NEW = ("single_with_partners", "single_negative_ev")


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
    ADD CONSTRAINT advisor_cycles_validation_check
    CHECK (validation_status IN ({_STATUSES}))
""")


def downgrade() -> None:
    blocking = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM advisor_cycles "
                "WHERE validation_status = ANY(:statuses)"
            ),
            {"statuses": list(_NEW)},
        )
        .scalar_one()
    )
    if blocking:
        raise RuntimeError(
            "cannot downgrade 0026_cycle_single_rules while cycles use "
            "single_with_partners or single_negative_ev"
        )
    _exec_sql(f"""
ALTER TABLE advisor_cycles
    DROP CONSTRAINT advisor_cycles_validation_check;

ALTER TABLE advisor_cycles
    ADD CONSTRAINT advisor_cycles_validation_check
    CHECK (validation_status IN ({_PREVIOUS}))
""")
