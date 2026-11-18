#!/bin/bash
# Runs once (postgres image entrypoint) on first start with an empty data volume.
# Creates the limited app login mi_app. Its table rights come from migration 0002
# ("uv run alembic upgrade head"), which never touches the role's login or password.
#
# The password is read inside psql with \getenv, so it never appears on a command line,
# in this file or in the logs.
set -eo pipefail

if [ -z "${PG_APP_PASSWORD:-}" ]; then
  echo "PG_APP_PASSWORD must be set in docker/compose.env" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_password PG_APP_PASSWORD
CREATE ROLE mi_app LOGIN PASSWORD :'app_password';
SQL
echo "created role mi_app (LOGIN)"
