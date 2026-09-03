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
# WELL-FORMED IS NOT THE SAME AS SUCCESSFUL. Parsing as JSON used to be the only
# test, so an HTTP 401, a FastAPI {"detail": ...} and a proxy's
# {"type":"error", ...} envelope all counted as success. Callers then read a body
# with no `results` key and reported "no results", turning a connection refusal
# into a confident empty answer. The status code is now checked, and error-shaped
# bodies are rejected, so a failure is always a non-zero exit.
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

# Usable means: parses as JSON, AND is not an error envelope. Anything an
# intermediary or the API itself returns to signal failure must fail here, or the
# caller reports it as data.
usable_json() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if isinstance(d, dict):
    # Proxy / gateway envelope, FastAPI validation error, and the common
    # {"error": ...} shape. A bare {"detail": null} is not an error.
    if d.get("type") == "error" or d.get("error") is not None:
        sys.exit(2)
    if d.get("detail") is not None and "results" not in d:
        sys.exit(2)
sys.exit(0)
PY
}

# Message worth showing the user, pulled out of whatever error shape came back.
error_message() {
  python3 - "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(open(sys.argv[1]).read()[:400]); sys.exit(0)
if isinstance(d, dict):
    e = d.get("error")
    if isinstance(e, dict):
        print(e.get("message") or json.dumps(e)[:400]); sys.exit(0)
    if e:
        print(str(e)[:400]); sys.exit(0)
    if d.get("detail") is not None:
        print(json.dumps(d["detail"])[:400]); sys.exit(0)
print(json.dumps(d)[:400])
PY
}

tmp=$(mktemp) || exit 1
code_file=$(mktemp) || exit 1
trap 'rm -f "$tmp" "$code_file"' EXIT

# -w writes the status to its own file: the body stays clean, and a non-2xx is
# visible even when the API returns a perfectly well-formed error document.
attempt_host() {
  if [[ -n "$BODY" ]]; then
    printf '%s' "$BODY" | curl -sS -m 300 -X "$METHOD" \
      "http://localhost:${PORT}${PATH_}" \
      "${AUTH[@]}" \
      -H 'Content-Type: application/json' --data-binary @- \
      -o "$tmp" -w '%{http_code}' >"$code_file" 2>/dev/null
  else
    curl -sS -m 300 -X "$METHOD" "http://localhost:${PORT}${PATH_}" "${AUTH[@]}" \
      -o "$tmp" -w '%{http_code}' >"$code_file" 2>/dev/null
  fi
}

# There is no second file to write the status to inside the container, so body and
# status share one stream and are split back apart here. Capture that stream into a
# variable FIRST: piping it into `{ body=$(sed '$d'); code=$(tail -n1); }` reads the
# same pipe twice, and `sed` must consume all of stdin to know which line is last,
# so `tail` always saw an exhausted pipe and `code` was always empty. That failed
# status_ok on every call, making this fallback unable to succeed -- and an empty
# code also lands on the "nothing answered" branch below, so a request that in fact
# returned 200 was reported as the stack being down.
attempt_container() {
  local out
  if [[ -n "$BODY" ]]; then
    out=$(printf '%s' "$BODY" | docker compose exec -T hindsight \
      curl -sS -m 300 -X "$METHOD" "http://localhost:8888${PATH_}" \
      "${AUTH[@]}" \
      -H 'Content-Type: application/json' --data-binary @- \
      -o /dev/stdout -w '\n%{http_code}' 2>/dev/null)
  else
    out=$(docker compose exec -T hindsight \
      curl -sS -m 300 -X "$METHOD" "http://localhost:8888${PATH_}" "${AUTH[@]}" \
      -o /dev/stdout -w '\n%{http_code}' 2>/dev/null)
  fi
  printf '%s' "$(sed '$d' <<<"$out")" >"$tmp"
  printf '%s' "$(tail -n1 <<<"$out")" >"$code_file"
}

status_ok() {
  local c
  c=$(cat "$code_file" 2>/dev/null)
  [[ "$c" =~ ^2[0-9][0-9]$ ]]
}

for attempt in attempt_host attempt_container; do
  : >"$tmp"; : >"$code_file"
  "$attempt"
  if [[ -s "$tmp" ]] && status_ok && usable_json "$tmp"; then
    cat "$tmp"
    exit 0
  fi
done

# Both paths failed. Say why, in the terms the caller needs: a status code, or the
# message out of the error body. Never print a partial body as though it were data.
code=$(cat "$code_file" 2>/dev/null)
echo "api call FAILED: $METHOD $PATH_" >&2
[[ -n "$code" && "$code" != "000" ]] && echo "  http status: $code" >&2
if [[ -s "$tmp" ]]; then
  msg=$(error_message "$tmp")
  [[ -n "$msg" ]] && echo "  response: $msg" >&2
fi
case "$code" in
  000|"") echo "  nothing answered on localhost:${PORT}. Is the stack up? try: make ps" >&2 ;;
  401|403) echo "  check HINDSIGHT_API_TENANT_API_KEY in .env" >&2 ;;
  *)       echo "  try: make ps    and: make logs" >&2 ;;
esac
exit 1
