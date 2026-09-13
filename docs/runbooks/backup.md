# Backup and restore

## Symptoms
`PredictorBackupFailed`, `PredictorBackupTooOld`, or `PredictorBackupOffsiteMissing`.

## Diagnose
`docker compose -f docker-compose.prod.yml logs backup`
Dumps live in volume `predictor_pgdumps`, not `predictor_pgdata`. Dump uses `--lock-wait-timeout=0` and never `pg_try_advisory_lock`.

## Action
- Failed dump: fix postgres connectivity; do not wait for the writer lock.
- Offsite missing: set `BACKUP_OFFSITE_CONFIGURED=true` and mount `BACKUP_OFFSITE_DIR` **outside** the production host disk (NAS/USB/object storage).
- Restore test: `scripts/backup/test-restore.sh` creates `predictor_restore_test` only. Never restore onto `predictor_prod` or the live `predictor_dev`.
- After a real restore onto a new host, start **one** worker; it acquires a new advisory lock.

## Resolved
`predictor_backup_last_success == 1` and last success timestamp younger than 24h. Offsite timestamp advances when offsite is configured.
