#!/bin/sh
# Provisions the non-owner runtime role for the nutrition schema.
# Runs once inside the postgres container on first init (empty data dir).
# NOTE: postgres:*-alpine has no bash - keep this POSIX and LF-only.
set -e

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<-SQL
    CREATE ROLE koko_app LOGIN PASSWORD '${NUTRITION_APP_PASSWORD}';
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO koko_app;
    GRANT USAGE ON SCHEMA public TO koko_app;
    -- No DELETE: published versions, ledger snapshots and audit stay append-oriented.
    GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO koko_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO koko_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE ON TABLES TO koko_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE ON SEQUENCES TO koko_app;
SQL
