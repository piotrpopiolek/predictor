# PostgreSQL down

## Symptoms
`PredictorPostgresDown`. Exporter or database unreachable.

## Diagnose
- `docker compose logs postgres`
- `pg_isready` in the postgres container.
- Disk full: see `docs/runbooks/disk.md`.

## Action
If the container is stopped, `up -d postgres` then worker. Never `down -v` on `predictor-prod`. Restore from backup only onto a clone (`docs/runbooks/backup.md`).

## Resolved
`up{job="postgres"} == 1` for 5 minutes and `/ready` postgres=true.
