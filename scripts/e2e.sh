#!/usr/bin/env bash
# Human-run end-to-end loop: trigger a lab failure, ship api logs and spans, investigate, print
# the report URL. Needs Docker. Never sources or prints .env. Leaves the stack up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE_URL="http://127.0.0.1:8000"
TIMEOUT="${E2E_TIMEOUT_SECONDS:-180}"

usage() {
  echo "usage: scripts/e2e.sh {schema-rename|stale-search-path}" >&2
  exit 2
}

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

NAME="${1:-}"
case "$NAME" in
  schema-rename | stale-search-path) ;;
  *) usage ;;
esac
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || fail "E2E_TIMEOUT_SECONDS must be a whole number of seconds"

if [[ ! -f .env ]]; then
  echo "error: .env is missing; run 'cp .env.example .env' and fill in fake values" >&2
  exit 1
fi

PROVIDER="$(grep -E '^CRE_LLM_PROVIDER=' .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
PROVIDER="${PROVIDER:-fake}"
echo "LLM provider: $PROVIDER"
if [[ "$PROVIDER" == "anthropic" ]]; then
  if ! grep -qE '^CRE_LLM_API_KEY=.+' .env || ! grep -qE '^CRE_LLM_MODEL=.+' .env; then
    echo "error: CRE_LLM_API_KEY and CRE_LLM_MODEL must be set in .env for anthropic" >&2
    exit 1
  fi
fi

BODY="$(mktemp)"
LOGS="$(mktemp)"
trap 'scripts/lab.sh reset || true; rm -f "$BODY" "$LOGS"' EXIT

scripts/lab.sh up || fail "lab up"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
scripts/lab.sh reset || fail "lab reset"
scripts/lab.sh trigger "$NAME" || fail "trigger $NAME"

REQUEST_ID="e2e-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
CODE="$(curl -s -o "$BODY" -w '%{http_code}' -H "X-Request-ID: $REQUEST_ID" "$BASE_URL/orders/1" || true)"
[[ "$CODE" == "500" ]] || fail "broken /orders/1 returned $CODE, expected 500"

TRACE_ID=""
SERVICE=""
SPAN_FOUND=0
for _ in $(seq 1 15); do
  docker compose logs --no-log-prefix --no-color --since "$START" api > "$LOGS"
  LINE="$(grep -F "\"request_id\": \"$REQUEST_ID\"" "$LOGS" | head -n 1 || true)"
  if [[ -n "$LINE" ]]; then
    TRACE_ID="$(grep -o '"trace_id": "[^"]*"' <<<"$LINE" | head -n 1 | sed 's/.*: "\(.*\)"/\1/')"
    SERVICE="$(grep -o '"service": "[^"]*"' <<<"$LINE" | head -n 1 | sed 's/.*: "\(.*\)"/\1/')"
    if [[ "$TRACE_ID" =~ ^[0-9a-f]{32}$ ]] && grep -qF "\"trace_id\": \"0x$TRACE_ID\"" "$LOGS"; then
      SPAN_FOUND=1
      break
    fi
  fi
  sleep 1
done
[[ "$TRACE_ID" =~ ^[0-9a-f]{32}$ ]] || fail "no trace_id found in api logs for request $REQUEST_ID"
[[ -n "$SERVICE" ]] || fail "no service found in api logs for request $REQUEST_ID"
if [[ "$SPAN_FOUND" != "1" ]]; then
  echo "warning: no span for trace $TRACE_ID appeared in time; shipping logs only" >&2
fi

echo "Shipping api logs and spans:"
docker compose run --rm -T api python -m cre.collectors stdout - --source-name api < "$LOGS" \
  || fail "collector stdout"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PAYLOAD="$(printf '{"timestamp": "%s", "trace_id": "%s", "service": "%s", "request_id": "%s", "symptom": "GET /orders/1 returned 500 (e2e %s)"}' \
  "$NOW" "$TRACE_ID" "$SERVICE" "$REQUEST_ID" "$NAME")"
CODE="$(curl -s -o "$BODY" -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "$PAYLOAD" "$BASE_URL/investigations" || true)"
[[ "$CODE" == "202" ]] || fail "POST /investigations returned $CODE, expected 202"
JOB_ID="$(grep -o '"id":"[^"]*"' "$BODY" | head -n 1 | sed 's/"id":"\(.*\)"/\1/')"
[[ -n "$JOB_ID" ]] || fail "no investigation id in the POST /investigations response"
echo "Investigation: $JOB_ID"

STATUS=""
DEADLINE=$((SECONDS + TIMEOUT))
while ((SECONDS < DEADLINE)); do
  CODE="$(curl -s -o "$BODY" -w '%{http_code}' "$BASE_URL/investigations/$JOB_ID" || true)"
  if [[ "$CODE" == "200" ]]; then
    STATUS="$(grep -o '"status":"[^"]*"' "$BODY" | head -n 1 | sed 's/"status":"\(.*\)"/\1/')"
    if [[ "$STATUS" == "completed" || "$STATUS" == "failed" ]]; then
      break
    fi
  fi
  sleep 2
done
if [[ "$STATUS" != "completed" && "$STATUS" != "failed" ]]; then
  fail "investigation $JOB_ID did not finish within ${TIMEOUT}s (last status: ${STATUS:-unknown})"
fi
if [[ "$STATUS" == "failed" ]]; then
  ERROR_CODE="$(grep -o '"error":"[^"]*"' "$BODY" | head -n 1 | sed 's/"error":"\(.*\)"/\1/')"
  fail "investigation $JOB_ID failed with error code ${ERROR_CODE:-unknown}"
fi

echo "Report: $BASE_URL/investigations/$JOB_ID/report"
if [[ "$PROVIDER" == "fake" ]]; then
  echo "note: the fake provider produces no hypotheses; the report shows the timeline and evidence only."
fi
