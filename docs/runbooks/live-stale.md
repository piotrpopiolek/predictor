# Live snapshots stale

## Symptoms
`PredictorLiveStale` while in-play fixtures exist.

## Diagnose
- Worker logs: `quota_exhausted`, `live_freshness_missed`, HTTP 429.
- Compare `predictor_live_snapshot_age_seconds` to `2 * predictor_live_poll_interval_seconds` (FR-019 stretches the interval when remaining quota cannot hold 60s until UTC midnight). `$value` is snapshot age, not in-play count.
- Ignore `captured_at` values ahead of database `now()` (pytest frozen clocks, API `update`).
- Host sleep (Windows) or Docker Desktop stopped.

## Action
Keep the worker running. Do not backfill live ticks. Report the gap; do not insert synthetic odds. Low remaining + `live_freshness_missed` is expected overnight; do not raise the daily plan in code.

## Resolved
New `fixture_odds_live` rows within two live poll intervals, or in-play count returns to 0.
