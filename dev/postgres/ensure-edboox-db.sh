#!/bin/sh
# Idempotently create the fake Edboox's OWN role + database inside the shared dev
# Postgres container (separate DB and user: the two apps never read each other's
# data). Safe on every `docker compose up`.
set -eu
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'edboox'" | grep -q 1 \
  || psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "CREATE ROLE edboox LOGIN PASSWORD 'edboox'"
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = 'edboox'" | grep -q 1 \
  || psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "CREATE DATABASE edboox OWNER edboox"
echo "edboox database ready"
