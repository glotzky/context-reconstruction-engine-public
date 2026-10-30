#!/usr/bin/env bash
# Human-run incident lab helper. Seeded failures are triggered only by cre.lab, never by alembic.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  echo "usage: scripts/lab.sh {up|trigger <name>|reset|down}" >&2
  exit 2
}

cmd="${1:-}"
case "$cmd" in
  up)
    if [[ ! -f .env ]]; then
      echo "error: .env is missing; run 'cp .env.example .env' and fill in fake values" >&2
      exit 1
    fi
    docker compose up -d --build --wait postgres
    docker compose run --rm api alembic upgrade head
    docker compose up -d --build --wait
    ;;
  trigger)
    name="${2:-}"
    [[ -n "$name" ]] || usage
    docker compose run --rm api python -m cre.lab apply "$name"
    if [[ "$name" == "stale-search-path" ]]; then
      # The role's search_path default only reaches new sessions, so restart the pooled api.
      docker compose restart api
      # restart returns before the api accepts connections; wait until it is healthy again.
      docker compose up -d --wait api
    fi
    ;;
  reset)
    docker compose run --rm api python -m cre.lab reset
    ;;
  down)
    docker compose down
    ;;
  *)
    usage
    ;;
esac
