#!/usr/bin/env bash
# Human-run end-to-end smoke test of the incident lab. Needs Docker; no API key. Leaves the stack up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE_URL="http://127.0.0.1:8000"
BODY="$(mktemp)"
trap 'scripts/lab.sh reset || true; rm -f "$BODY"' EXIT

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

status() {
  curl -s -o "$BODY" -w '%{http_code}' "$BASE_URL$1" || true
}

scripts/lab.sh up || fail "lab up"

[[ "$(status /health)" == "200" ]] || fail "/health is not 200"
[[ "$(status /orders/1)" == "200" ]] || fail "healthy /orders/1 is not 200"

scripts/lab.sh trigger schema-rename || fail "trigger schema-rename"
[[ "$(status /orders/1)" == "500" ]] || fail "broken /orders/1 is not 500"
grep -q "UndefinedColumn" "$BODY" || fail "broken /orders/1 body has no UndefinedColumn"

scripts/lab.sh reset || fail "lab reset"
[[ "$(status /orders/1)" == "200" ]] || fail "/orders/1 is not 200 after reset"

echo "PASS"
