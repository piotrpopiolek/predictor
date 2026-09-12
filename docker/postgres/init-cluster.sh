#!/bin/bash
set -euo pipefail

# Creates predictor_prod / predictor_dev and roles. Runs only on first init of the data directory.

escape_sql() {
  printf "%s" "$1" | sed "s/'/''/g"
}

: "${PREDICTOR_APP_PASSWORD:?PREDICTOR_APP_PASSWORD is required}"
: "${PREDICTOR_DEV_PASSWORD:?PREDICTOR_DEV_PASSWORD is required}"

APP_PWD="$(escape_sql "${PREDICTOR_APP_PASSWORD}")"
DEV_PWD="$(escape_sql "${PREDICTOR_DEV_PASSWORD}")"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<SQL
CREATE ROLE predictor_app LOGIN PASSWORD '${APP_PWD}';
CREATE ROLE predictor_dev LOGIN PASSWORD '${DEV_PWD}';

CREATE DATABASE predictor_prod OWNER predictor_app;
CREATE DATABASE predictor_dev OWNER predictor_dev;

REVOKE CONNECT ON DATABASE predictor_prod FROM PUBLIC;
GRANT CONNECT ON DATABASE predictor_prod TO predictor_app;

REVOKE CONNECT ON DATABASE predictor_dev FROM PUBLIC;
GRANT CONNECT ON DATABASE predictor_dev TO predictor_dev;
SQL
