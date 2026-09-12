"""Process-wide constants. The writer lock key is shared by worker and status."""

WRITER_LOCK_KEY = 739201
LOCK_KEEPALIVES_IDLE_SECONDS = 20
LOCK_KEEPALIVES_INTERVAL_SECONDS = 5
LOCK_KEEPALIVES_COUNT = 3
LOCK_APPLICATION_NAME = "predictor-worker-lock"

HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
HTTP_READ_TIMEOUT_SECONDS = 30.0
HTTP_WRITE_TIMEOUT_SECONDS = 10.0
HTTP_POOL_TIMEOUT_SECONDS = 10.0
HTTP_MAX_CONNECTIONS = 10
HTTP_MAX_KEEPALIVE_CONNECTIONS = 5
HTTP_RETRY_ATTEMPTS = 5

# Priorities 1–3 (quota/live dictionaries, live fixtures, next-goal) may spend
# the configured safety buffer. Forward/backfill/enrichment/global may not.
LIVE_QUOTA_PRIORITIES = frozenset({1, 2, 3})

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

API_SPORTS_KEY_HEADER = "x-apisports-key"

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
