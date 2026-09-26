#!/bin/sh
# Runs once, on first init, via postgres's /docker-entrypoint-initdb.d/ convention.
# Gives the LLM gateway its OWN database and role (red-team #2 item 8) instead of
# reusing the controller's `factory` user -- a compromised gateway process then
# can't read or write the controller's mission/approval/intent tables.
set -eu

password_file="${LITELLM_PASSWORD_FILE:?LITELLM_PASSWORD_FILE must be set}"
litellm_password="$(cat "$password_file")"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER litellm WITH PASSWORD '${litellm_password}';
    CREATE DATABASE litellm OWNER litellm;
EOSQL
