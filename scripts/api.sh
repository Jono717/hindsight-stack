#!/usr/bin/env bash
# One API call, via whichever path actually returns complete JSON.
#
# Usage:  api.sh GET  /v1/default/banks
#         api.sh POST /v1/default/banks/test/memories '{"items":[...]}'
#
# Why this exists: some environments run an HTTP proxy that rewrites localhost
# traffic and truncates larger responses, which surfaces as invalid JSON rather
# than as an error. The API itself is fine -- a direct socket request returns the
# full payload -- so this tries the host first and silently falls back to running
# the same request inside the container, which no host proxy can touch.
#
# Prints the response body on stdout. Exits non-zero if neither path works.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

METHOD="${1:?usage: api.sh METHOD PATH [BODY]}"
PATH_="${2:?usage: api.sh METHOD PATH [BODY]}"
BODY="${3:-}"

PORT="${HINDSIGHT_API_PORT:-8888}"
[[ -f .env ]] && PORT=$(grep -E '^HINDSIGHT_API_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "$PORT")
PORT="${PORT:-8888}"

# The API is key-protected when HINDSIGHT_API_TENANT_API_KEY is set. Read it from
# .env so every make target authenticates without the key appearing on a command
# line (where it would land in shell history and ps output).
KEY="${HINDSIGHT_API_TENANT_API_KEY:-}"
if [[ -z "$KEY" && -f .env ]]; then
  KEY=$(grep -E '^HINDSIGHT_API_TENANT_API_KEY=' .env 2>/dev/null | cut -d= -f2-)
fi
AUTH=()
[[ -n "$KEY" ]] && AUTH=(-H "Authorization: Bearer ${KEY}")

valid_json() { python3 -c 'import json,sys; json.load(sys.stdin)' <"$1" 2>/dev/null; }

tmp=$(mktemp) || exit 1
trap 'rm -f "$tmp"' EXIT

# Attempt 1: from the host.
if [[ -n "$BODY" ]]; then
  printf '%s' "$BODY" | curl -sS -m 300 -X "$METHOD" \
    "http://localhost:${PORT}${PATH_}" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' --data-binary @- >"$tmp" 2>/dev/null
else
  curl -sS -m 300 -X "$METHOD" "http://localhost:${PORT}${PATH_}" "${AUTH[@]}" >"$tmp" 2>/dev/null
fi

if [[ -s "$tmp" ]] && valid_json "$tmp"; then
  cat "$tmp"
  exit 0
fi

# Attempt 2: inside the container, out of reach of any host proxy.
if [[ -n "$BODY" ]]; then
  printf '%s' "$BODY" | docker compose exec -T hindsight \
    curl -sS -m 300 -X "$METHOD" "http://localhost:8888${PATH_}" \
    "${AUTH[@]}" \
    -H 'Content-Type: application/json' --data-binary @- >"$tmp" 2>/dev/null
else
  docker compose exec -T hindsight \
    curl -sS -m 300 -X "$METHOD" "http://localhost:8888${PATH_}" "${AUTH[@]}" >"$tmp" 2>/dev/null
fi

if [[ -s "$tmp" ]] && valid_json "$tmp"; then
  cat "$tmp"
  exit 0
fi

# Both failed. Show whatever came back so the cause is visible.
echo "api call failed: $METHOD $PATH_" >&2
[[ -s "$tmp" ]] && { echo "last response:" >&2; head -c 400 "$tmp" >&2; echo >&2; }
echo "is the stack up? try: make ps" >&2
exit 1
