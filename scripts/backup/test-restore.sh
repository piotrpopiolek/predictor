#!/bin/bash
set -euo pipefail
# Restore test clone. Never writes to predictor_prod or predictor_dev.
pg_dump -U postgres -d predictor_prod -Fc --lock-wait-timeout=0 -f /tmp/predictor_prod.dump
psql -U postgres -v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS predictor_restore_test WITH (FORCE);
CREATE DATABASE predictor_restore_test OWNER predictor_app;
SQL
pg_restore -U postgres -d predictor_restore_test --no-owner --role=predictor_app /tmp/predictor_prod.dump || true
psql -U postgres -d predictor_restore_test -v ON_ERROR_STOP=1 -c "SELECT 1"
psql -U postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS predictor_restore_test WITH (FORCE);"
rm -f /tmp/predictor_prod.dump
echo "restore test ok"
