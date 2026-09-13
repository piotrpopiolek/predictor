# Worker heartbeat missing

## Symptoms
`PredictorWorkerHeartbeatMissing` or `PredictorStatusDown`. Writer lock gauge is 0, or `/metrics` scrape fails.

## Diagnose
- `docker compose -f docker-compose.prod.yml logs worker status`
- `GET /ready` on status (loopback). Check advisory lock in `pg_locks`.
- 401/403 from API-Football exits the worker (`auth_blocked`).

## Action
Restart only the worker after the lock session is gone. Do not start a second writer. If auth failed, rotate `API_SPORTS_KEY`.

## Resolved
`predictor_writer_lock == 1` and status scrape `up == 1` for 5 minutes.
