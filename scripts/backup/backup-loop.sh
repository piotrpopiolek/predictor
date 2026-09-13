#!/bin/bash
set -euo pipefail
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
while true; do
  if ! /scripts/backup-once.sh; then
    NOW="$(date -u +%s)"
    mkdir -p /backups
    cat > /backups/predictor_backup.prom <<EOF
# HELP predictor_backup_last_success 1 after a successful dump.
# TYPE predictor_backup_last_success gauge
predictor_backup_last_success 0
# HELP predictor_backup_last_attempt_timestamp Unix time of last backup attempt.
# TYPE predictor_backup_last_attempt_timestamp gauge
predictor_backup_last_attempt_timestamp ${NOW}
EOF
  fi
  sleep "$INTERVAL"
done
