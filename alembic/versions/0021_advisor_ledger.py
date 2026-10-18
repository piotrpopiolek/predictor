"""Advisor decision ledger: strategies, cycles, coupons. Outside the ETL mirror."""

from alembic import op

revision: str = "0021_advisor_ledger"
down_revision: str | None = "0020_untracked_leagues"
branch_labels: str | None = None
depends_on: str | None = None


def _exec_sql(script: str) -> None:
    for raw in script.split(";"):
        statement = raw.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_sql("""
CREATE TABLE advisor_strategies (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(128) NOT NULL UNIQUE,
    status      VARCHAR(16) NOT NULL DEFAULT 'proposed',
    kind        VARCHAR(16) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_strategies_status_check CHECK (
        status IN ('proposed', 'sandbox', 'promoted', 'paused', 'retired')
    ),
    CONSTRAINT advisor_strategies_kind_check CHECK (kind IN ('llm', 'baseline'))
);

CREATE UNIQUE INDEX uq_advisor_strategies_one_promoted
    ON advisor_strategies (status)
    WHERE status = 'promoted';

CREATE TABLE advisor_strategy_versions (
    id                     BIGSERIAL PRIMARY KEY,
    strategy_id            BIGINT NOT NULL REFERENCES advisor_strategies(id) ON DELETE RESTRICT,
    version_no             INT NOT NULL,
    prompt_version         VARCHAR(64),
    min_combined_odd       NUMERIC(6,3) NOT NULL,
    max_legs               INT NOT NULL,
    max_minute_regulation  INT NOT NULL,
    max_minute_extra       INT NOT NULL,
    wait_minutes           INT NOT NULL,
    candidate_rules        JSONB NOT NULL,
    notes                  TEXT NOT NULL DEFAULT '',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_advisor_strategy_versions UNIQUE (strategy_id, version_no),
    CONSTRAINT advisor_strategy_versions_version_check CHECK (version_no >= 1),
    CONSTRAINT advisor_strategy_versions_odd_check CHECK (min_combined_odd > 1),
    CONSTRAINT advisor_strategy_versions_legs_check CHECK (max_legs BETWEEN 1 AND 3),
    CONSTRAINT advisor_strategy_versions_minute_check
        CHECK (max_minute_regulation BETWEEN 1 AND 120),
    CONSTRAINT advisor_strategy_versions_extra_minute_check
        CHECK (max_minute_extra BETWEEN 91 AND 130),
    CONSTRAINT advisor_strategy_versions_wait_check CHECK (wait_minutes BETWEEN 1 AND 30)
);

CREATE TABLE advisor_cycles (
    id                   BIGSERIAL PRIMARY KEY,
    strategy_version_id  BIGINT NOT NULL
        REFERENCES advisor_strategy_versions(id) ON DELETE RESTRICT,
    mode                 VARCHAR(16) NOT NULL,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at          TIMESTAMPTZ,
    provider             VARCHAR(32) NOT NULL,
    model                VARCHAR(64) NOT NULL,
    prompt_version       VARCHAR(64) NOT NULL,
    prompt_text          TEXT NOT NULL,
    input_json           JSONB NOT NULL,
    raw_output           TEXT,
    validation_status    VARCHAR(16) NOT NULL,
    input_tokens         INT NOT NULL DEFAULT 0,
    output_tokens        INT NOT NULL DEFAULT 0,
    cost_pln             NUMERIC(12,4) NOT NULL,
    latency_ms           INT,
    langfuse_trace_id    VARCHAR(64),
    otel_trace_id        VARCHAR(64),
    CONSTRAINT advisor_cycles_mode_check
        CHECK (mode IN ('sandbox', 'recommendation')),
    CONSTRAINT advisor_cycles_validation_check CHECK (
        validation_status IN ('ok', 'invalid_output', 'provider_error')
    ),
    CONSTRAINT advisor_cycles_cost_check CHECK (cost_pln >= 0)
);

CREATE INDEX idx_advisor_cycles_started ON advisor_cycles (started_at);

CREATE TABLE advisor_decisions (
    id                     BIGSERIAL PRIMARY KEY,
    cycle_id               BIGINT REFERENCES advisor_cycles(id) ON DELETE RESTRICT,
    strategy_id            BIGINT NOT NULL REFERENCES advisor_strategies(id) ON DELETE RESTRICT,
    fixture_id             BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE RESTRICT,
    period                 VARCHAR(16) NOT NULL,
    goals_home             INT NOT NULL,
    goals_away             INT NOT NULL,
    ordinal                INT NOT NULL,
    candidate_rule         VARCHAR(32) NOT NULL,
    decision               VARCHAR(16) NOT NULL,
    reasons                JSONB NOT NULL,
    rationale              TEXT NOT NULL DEFAULT '',
    estimated_probability  NUMERIC(6,5),
    confidence             NUMERIC(6,5),
    odd_at_eval            NUMERIC(10,3),
    n                      INT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_decisions_period_check
        CHECK (period IN ('regulation', 'extra_time')),
    CONSTRAINT advisor_decisions_decision_check
        CHECK (decision IN ('bet', 'reject', 'wait', 'wait_timeout')),
    CONSTRAINT advisor_decisions_probability_check CHECK (
        estimated_probability IS NULL
        OR (estimated_probability >= 0 AND estimated_probability <= 1)
    ),
    CONSTRAINT advisor_decisions_confidence_check CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    )
);

CREATE UNIQUE INDEX uq_advisor_decisions_rejected_state
    ON advisor_decisions (
        strategy_id, fixture_id, period, goals_home, goals_away, ordinal
    )
    WHERE decision IN ('reject', 'wait_timeout');

CREATE TABLE advisor_counterfactuals (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   BIGINT NOT NULL UNIQUE
        REFERENCES advisor_decisions(id) ON DELETE RESTRICT,
    goal_scored   BOOLEAN NOT NULL,
    odd           NUMERIC(10,3),
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE advisor_coupons (
    id                     BIGSERIAL PRIMARY KEY,
    mode                   VARCHAR(16) NOT NULL,
    strategy_version_id    BIGINT NOT NULL
        REFERENCES advisor_strategy_versions(id) ON DELETE RESTRICT,
    cycle_id               BIGINT REFERENCES advisor_cycles(id) ON DELETE RESTRICT,
    status                 VARCHAR(16) NOT NULL,
    stake                  NUMERIC(12,2) NOT NULL,
    combined_odd           NUMERIC(10,3),
    proposed_combined_odd  NUMERIC(10,3),
    payout_keep            NUMERIC(4,3) NOT NULL DEFAULT 0.880,
    placed_at              TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_coupons_mode_check
        CHECK (mode IN ('sandbox', 'recommendation')),
    CONSTRAINT advisor_coupons_status_check CHECK (status IN (
        'open', 'proposed', 'accepted', 'modified', 'rejected', 'expired',
        'placed', 'not_placed', 'won', 'lost', 'void'
    )),
    CONSTRAINT advisor_coupons_stake_check CHECK (stake > 0),
    CONSTRAINT advisor_coupons_odd_check
        CHECK (combined_odd IS NULL OR combined_odd > 1),
    CONSTRAINT advisor_coupons_keep_check
        CHECK (payout_keep > 0 AND payout_keep <= 1)
);

CREATE INDEX idx_advisor_coupons_version ON advisor_coupons (strategy_version_id);
CREATE INDEX idx_advisor_coupons_status ON advisor_coupons (status);

CREATE TABLE advisor_legs (
    id            BIGSERIAL PRIMARY KEY,
    coupon_id     BIGINT NOT NULL REFERENCES advisor_coupons(id) ON DELETE RESTRICT,
    strategy_id   BIGINT NOT NULL REFERENCES advisor_strategies(id) ON DELETE RESTRICT,
    fixture_id    BIGINT NOT NULL REFERENCES fixtures(id) ON DELETE RESTRICT,
    mode          VARCHAR(16) NOT NULL,
    period        VARCHAR(16) NOT NULL,
    n             INT NOT NULL,
    odd           NUMERIC(10,3) NOT NULL,
    proposed_odd  NUMERIC(10,3),
    goals_home    INT NOT NULL,
    goals_away    INT NOT NULL,
    elapsed       INT,
    status        VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_advisor_legs_coupon_fixture UNIQUE (coupon_id, fixture_id),
    CONSTRAINT advisor_legs_period_check
        CHECK (period IN ('regulation', 'extra_time')),
    CONSTRAINT advisor_legs_mode_check
        CHECK (mode IN ('sandbox', 'recommendation')),
    CONSTRAINT advisor_legs_status_check CHECK (
        status IN ('open', 'won', 'lost', 'void', 'removed', 'manual')
    ),
    CONSTRAINT advisor_legs_n_check CHECK (n >= 0),
    CONSTRAINT advisor_legs_odd_check CHECK (odd > 1)
);

CREATE UNIQUE INDEX uq_advisor_legs_open_sandbox
    ON advisor_legs (strategy_id, fixture_id)
    WHERE status IN ('open', 'manual') AND mode = 'sandbox';

CREATE UNIQUE INDEX uq_advisor_legs_open_recommendation
    ON advisor_legs (fixture_id)
    WHERE status IN ('open', 'manual') AND mode = 'recommendation';

CREATE TABLE advisor_settlement_events (
    id               BIGSERIAL PRIMARY KEY,
    coupon_id        BIGINT NOT NULL REFERENCES advisor_coupons(id) ON DELETE RESTRICT,
    leg_id           BIGINT REFERENCES advisor_legs(id) ON DELETE RESTRICT,
    previous_status  VARCHAR(16) NOT NULL,
    new_status       VARCHAR(16) NOT NULL,
    source           VARCHAR(16) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT advisor_settlement_events_source_check
        CHECK (source IN ('settle', 'correction', 'manual'))
)
""")


def downgrade() -> None:
    _exec_sql("""
DROP TABLE IF EXISTS advisor_settlement_events;
DROP TABLE IF EXISTS advisor_legs;
DROP TABLE IF EXISTS advisor_coupons;
DROP TABLE IF EXISTS advisor_counterfactuals;
DROP TABLE IF EXISTS advisor_decisions;
DROP TABLE IF EXISTS advisor_cycles;
DROP TABLE IF EXISTS advisor_strategy_versions;
DROP TABLE IF EXISTS advisor_strategies
""")
