# Live snapshots stale

## Symptoms
`PredictorLiveStale` while in-play fixtures exist.

## Diagnose
- Worker logs: `quota_exhausted`, `live_freshness_missed`, HTTP 429.
- `predictor_in_play_fixtures` and `predictor_live_snapshot_age_seconds`.
- Ignore `captured_at` values ahead of database `now()` (pytest frozen clocks, API `update`).
- Host sleep (Windows) or Docker Desktop stopped.

## Action
Keep the worker running. Do not backfill live ticks. Report the gap; do not insert synthetic odds.

## Resolved
New `fixture_odds_live` rows within two poll intervals, or in-play count returns to 0.
