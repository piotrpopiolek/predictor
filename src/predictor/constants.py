"""Process-wide constants. The writer lock key is shared by worker and status."""

from decimal import ROUND_HALF_UP, Decimal

WRITER_LOCK_KEY = 739201
ADVISOR_LOCK_KEY = 739202
# Serializes one budget check plus its model call. Not the process-lifetime lock.
ADVISOR_CYCLE_LOCK_KEY = 739203
PAYOUT_KEEP = Decimal("0.880")
# Virtual portfolio (FR-120). The balance is the sum of coupons, not a stored column.
SANDBOX_BANKROLL = Decimal("200.00")
# Same stake rule as the operator ledger: 21.00, then +0.10 per settled win.
START_STAKE = Decimal("21.00")
STAKE_STEP = Decimal("0.10")
LOCK_KEEPALIVES_IDLE_SECONDS = 20
LOCK_KEEPALIVES_INTERVAL_SECONDS = 5
LOCK_KEEPALIVES_COUNT = 3
LOCK_APPLICATION_NAME = "predictor-worker-lock"
ADVISOR_LOCK_APPLICATION_NAME = "predictor-advisor-lock"


def stake_after_wins(wins: int) -> Decimal:
    """Stake for the next bet. Losses and voids do not change it."""
    return (START_STAKE + STAKE_STEP * wins).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
HTTP_READ_TIMEOUT_SECONDS = 30.0
HTTP_WRITE_TIMEOUT_SECONDS = 10.0
HTTP_POOL_TIMEOUT_SECONDS = 10.0
HTTP_MAX_CONNECTIONS = 10
HTTP_MAX_KEEPALIVE_CONNECTIONS = 5
HTTP_RETRY_ATTEMPTS = 5

# Last calls kept for the next /fixtures?live=all poll. Context and odds
# stop before this. History stops earlier, once the live reserve is touched.
LIVE_REQUESTS_PER_TICK = 2

# P3 live-context budget after /fixtures?live=all.
# Detail (/fixtures?id=) repeats every 5 minutes for events and match stats.
# One-shots (odds, predictions, H2H, squads, season stats) run once, a few per tick.
LIVE_DETAIL_REFRESH_SECONDS = 5 * 60
LIVE_DETAIL_REFRESH_MAX_SECONDS = 15 * 60
# A match still to be played is reserved for about two hours of detail polls.
MATCH_LIVE_HOURS = 2
IDLE_SCORE_POLL_SECONDS = 5 * 60
# Do not enqueue another historical wave while an open task is older than this.
HISTORICAL_BACKLOG_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
LIVE_CONTEXT_DETAIL_CALLS = 40
LIVE_CONTEXT_FINAL_CALLS = 8
LIVE_CONTEXT_ONESHOT_CALLS = 12
LIVE_CONTEXT_MAX_CALLS_PER_TICK = (
    LIVE_CONTEXT_DETAIL_CALLS + LIVE_CONTEXT_FINAL_CALLS + LIVE_CONTEXT_ONESHOT_CALLS
)
LIVE_CONTEXT_TIME_FRACTION = 0.85
LIVE_CONTEXT_CREDIT_TICKS = 3
LIVE_CONTEXT_EMPTY_BACKOFF_SECONDS = 5 * 60
LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS = 5
# A failed fetch goes back on the queue this many times, then permanent_error.
ERROR_RETRY_LIMIT = 10
ERROR_RETRY_BASE_BACKOFF_SECONDS = 60
ERROR_RETRY_MAX_BACKOFF_SECONDS = 60 * 60

CURSOR_KINDS = ("forward", "backfill", "enrichment")

ETL_STATUSES = frozenset(
    {
        "pending",
        "in_progress",
        "complete",
        "coverage_empty",
        "not_supported",
        "retryable_error",
        "permanent_error",
    }
)
OPEN_ETL_STATUSES = frozenset({"pending", "in_progress", "retryable_error"})
DAILY_REPORT_ENDPOINT = "report/daily"
QUOTA_SNAPSHOT_ENDPOINT = "quota/snapshot"
DAY_CONTRACT_ENDPOINTS = frozenset(
    {
        "/fixtures",
        "/fixtures/statistics",
        "/fixtures/headtohead",
        "/predictions",
        "/odds",
    }
)

API_SPORTS_KEY_HEADER = "x-apisports-key"

# FR-014: stored with fixtures; W6 marks irregular enrichment coverage_empty.
IRREGULAR_FIXTURE_STATUSES = frozenset({"PST", "CANC", "ABD", "AWD", "WO"})
FINISHED_FIXTURE_STATUSES = frozenset({"FT", "AET", "PEN"})
IN_PLAY_FIXTURE_STATUSES = frozenset(
    {"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"}
)
ENRICHABLE_FIXTURE_STATUSES = FINISHED_FIXTURE_STATUSES | IRREGULAR_FIXTURE_STATUSES
HALF_STATS_FROM_SEASON = 2024
CONTROL_REFRESH_HOURS = 24
ENRICHMENT_PER_TICK = 6
PREMATCH_PER_TICK = 4
URGENT_PREMATCH_PER_TICK = 4
URGENT_PREMATCH_HORIZON_HOURS = 6
GLOBAL_PER_TICK = 3
HISTORY_FETCH_CONCURRENCY = 8
LIVE_DETAIL_CONCURRENCY = 2
ODDS_MAPPING_RETRY_BACKOFF_SECONDS = 15 * 60
ODDS_MAPPING_REFRESH_SECONDS = 2 * 60 * 60

# ETL sentinel for /teams/statistics without a date filter (schema §2.6).
TEAM_STATS_SENTINEL_DATE = "1970-01-01"

# Reconstructable lookups: never HTTP (T087 / T083 seasons).
LOOKUP_WITHOUT_HTTP: frozenset[str] = frozenset({"/teams/seasons", "/players/seasons"})

TOP_PLAYER_ENDPOINTS: tuple[str, ...] = (
    "/players/topscorers",
    "/players/topassists",
    "/players/topyellowcards",
    "/players/topredcards",
)

# Drain order inside runtime priority 8 (after pre-match).
GLOBAL_ENDPOINT_ORDER: tuple[str, ...] = (
    "/standings",
    "/teams",
    "/venues",
    "/teams/statistics",
    "/players",
    "/players/profiles",
    "/players/squads",
    "/players/teams",
    *TOP_PLAYER_ENDPOINTS,
    "/coachs",
    "/transfers",
    "/trophies",
    "/sidelined",
)

# Snapshot refresh is off while the history queue drains. The stored row stays.
# Reopening /standings, /teams/statistics, /players, /players/squads, and
# the four top lists every UTC day was spending the history budget on
# overwrites. An empty set makes requeue_stale_global a no-op.
STALE_GLOBAL_ENDPOINTS: frozenset[str] = frozenset()

# W3 catalog. Order is FK-safe: countries before leagues. No /fixtures.
DICTIONARY_ENDPOINTS: tuple[str, ...] = (
    "/timezone",
    "/countries",
    "/teams/countries",
    "/leagues/seasons",
    "/leagues",
    "/odds/bookmakers",
    "/odds/bets",
    "/odds/live/bets",
)
