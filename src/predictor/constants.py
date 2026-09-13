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

# Forecast for FR-019: one /fixtures?live=all plus one /odds/live per tick
# (extra pages are not reserved; quota_exhausted stops mid-page).
LIVE_REQUESTS_PER_TICK = 2

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

# FR-014: stored with fixtures; W6 marks irregular enrichment coverage_empty.
IRREGULAR_FIXTURE_STATUSES = frozenset({"PST", "CANC", "ABD", "AWD", "WO"})
FINISHED_FIXTURE_STATUSES = frozenset({"FT", "AET", "PEN"})
ENRICHABLE_FIXTURE_STATUSES = FINISHED_FIXTURE_STATUSES | IRREGULAR_FIXTURE_STATUSES
HALF_STATS_FROM_SEASON = 2024
CONTROL_REFRESH_HOURS = 24
ENRICHMENT_PER_TICK = 6
PREMATCH_PER_TICK = 4
GLOBAL_PER_TICK = 3

# ETL sentinel for /teams/statistics without a date filter (schema §2.6).
TEAM_STATS_SENTINEL_DATE = "1970-01-01"

# Reconstructable lookups: never HTTP (T087 / T083 seasons).
LOOKUP_WITHOUT_HTTP: frozenset[str] = frozenset(
    {"/teams/seasons", "/players/seasons"}
)

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

# Snapshot endpoints re-queued the next UTC day.
STALE_GLOBAL_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/standings",
        "/teams/statistics",
        "/players",
        "/players/squads",
        *TOP_PLAYER_ENDPOINTS,
    }
)

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
