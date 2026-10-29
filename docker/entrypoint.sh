#!/bin/sh
# Container start-up shared by web and worker. Only the web service sets RUN_MIGRATIONS, so
# two containers never race to migrate the same database.
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python manage.py migrate --noinput
    if [ "${SEED_DEMO:-0}" = "1" ]; then
        python manage.py seed_demo --if-empty
    fi
fi

exec "$@"
