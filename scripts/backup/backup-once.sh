#!/bin/bash
set -euo pipefail
# Dump predictor_prod without waiting on table locks. Advisory locks are not dump locks.
: "${PGHOST:=postgres}"
: "${PGUSER:=postgres}"
DEST=/backups
mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="${DEST}/predictor_prod_${STAMP}.dump"
pg_dump -h "$PGHOST" -U "$PGUSER" -d predictor_prod -Fc --lock-wait-timeout=0 -f "$DUMP"
ls -1t "${DEST}"/predictor_prod_*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f
NOW="$(date -u +%s)"
OFFSITE_TS=0
CONFIGURED=0
if [ "${BACKUP_OFFSITE_CONFIGURED:-false}" = "true" ]; then
  CONFIGURED=1
  if [ ! -d /offsite ] || [ ! -w /offsite ]; then
    echo "offsite directory missing or not writable" >&2
    exit 1
  fi
  cp "$DUMP" /offsite/
  OFFSITE_TS="$NOW"
fi
cat > "${DEST}/predictor_backup.prom" <<EOF
# HELP predictor_backup_last_success_timestamp Unix time of last successful predictor_prod dump.
# TYPE predictor_backup_last_success_timestamp gauge
predictor_backup_last_success_timestamp ${NOW}
# HELP predictor_backup_last_offsite_timestamp Unix time of last offsite copy.
# TYPE predictor_backup_last_offsite_timestamp gauge
predictor_backup_last_offsite_timestamp ${OFFSITE_TS}
# HELP predictor_backup_offsite_configured 1 if offsite copy is configured.
# TYPE predictor_backup_offsite_configured gauge
predictor_backup_offsite_configured ${CONFIGURED}
# HELP predictor_backup_last_success 1 after a successful dump.
# TYPE predictor_backup_last_success gauge
predictor_backup_last_success 1
EOF
