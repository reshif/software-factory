#!/bin/sh
# Builds FACTORY_DATABASE_URL from a Docker/Compose secret file rather than a
# plain-text environment variable (red-team #2 item 8), then execs the real
# command (`factory serve`/`factory worker`). `docker inspect`/`compose config`
# never show the password this way -- only this script (running inside the
# container) ever reads the secret file.
set -eu

if [ -n "${POSTGRES_PASSWORD_FILE:-}" ] && [ -f "$POSTGRES_PASSWORD_FILE" ]; then
    password="$(cat "$POSTGRES_PASSWORD_FILE")"
    export FACTORY_DATABASE_URL="postgresql://${POSTGRES_USER:-factory}:${password}@${POSTGRES_HOST:-postgres}:5432/${POSTGRES_DB:-factory}"
fi

exec "$@"
