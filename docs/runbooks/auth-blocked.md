# API authorization blocked

## Symptoms
`PredictorApiAuthBlocked`. Worker dropped the writer lock while status still scrapes.

## Diagnose
Worker log event `auth_blocked`. HTTP 401/403 from API-Football stops further domain requests.

## Action
Fix `API_SPORTS_KEY`. Restart a **single** worker after the old lock session is gone.

## Resolved
Worker holds the lock and completes a `/status` quota fetch without `AuthBlockedError`.
