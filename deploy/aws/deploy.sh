#!/usr/bin/env bash
# Run on the server to ship the latest code: pull, rebuild, restart. The web container
# applies migrations on start and the worker waits for it.
set -euo pipefail
cd "$(dirname "$0")/../.."

compose=(docker compose -f compose.prod.yaml --env-file .env.production)
[ -f .env.production ] || { echo ".env.production is missing; see docs/DEPLOY_AWS.md" >&2; exit 1; }

git pull --ff-only
"${compose[@]}" up -d --build --remove-orphans
docker image prune -f >/dev/null
"${compose[@]}" ps
